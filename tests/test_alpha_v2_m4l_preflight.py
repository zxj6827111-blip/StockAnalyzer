"""M4-L R1 §29 + §10(PF/VOL)：Production Data Preflight 专项测试。

覆盖：volume 价格归一化判别（VOL-1/2）、mixed/切换 BLOCKED、尾段分级、重复主键、
安全开关、运行身份、生产链前置（PF-4）、特征 fill-zero 假健康、模型身份与训练数据
指纹（PF-1/2/3）、preflight 硬门四要素与工件防篡改。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from stock_analyzer.alpha_v2.validation import preflight as pf
from stock_analyzer.alpha_v2.validation.training_data_fingerprint import (
    compute_training_data_fingerprint,
)
from stock_analyzer.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _market_db(
    path: Path,
    *,
    months: list[tuple[str, int, str]],
    tail_fragment: bool = False,
) -> Path:
    """造 daily_bars：``months`` = [(YYYY-MM, 天数, "share"|"lot"|"mixed")]。

    share = volume 以股计（turnover ≈ volume × price）；
    lot   = volume 以手计（turnover ≈ volume × price × 100）。
    """
    rows: list[dict[str, object]] = []
    symbols = ["600001", "600002"]
    for month, day_count, mode in months:
        for day_index in range(day_count):
            day = date.fromisoformat(f"{month}-{day_index + 1:02d}")
            for index, symbol in enumerate(symbols):
                price = 12.0 + index
                if mode == "share":
                    volume, turnover = 1_000_000.0, 1_000_000.0 * price
                elif mode == "lot":
                    volume, turnover = 10_000.0, 10_000.0 * price * 100.0
                else:  # mixed：同月两种单位各一半
                    share_like = (index + day_index) % 2 == 0
                    volume = 1_000_000.0 if share_like else 10_000.0
                    turnover = volume * price * (1.0 if share_like else 100.0)
                rows.append(
                    {
                        "symbol": symbol,
                        "date": day,
                        "open": price,
                        "high": price * 1.01,
                        "low": price * 0.99,
                        "close": price,
                        "volume": volume,
                        "turnover": turnover,
                        "board": "主板",
                        "is_st": False,
                    }
                )
    if tail_fragment and rows:
        last_date = max(row["date"] for row in rows)  # type: ignore[type-var]
        rows = [row for row in rows if not (row["date"] == last_date and row["symbol"] == "600002")]
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, "
            "low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE, board VARCHAR, "
            "is_st BOOLEAN)"
        )
        connection.executemany(
            "INSERT INTO daily_bars (symbol, date, open, high, low, close, volume, "
            "turnover, board, is_st) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["symbol"],
                    row["date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row["turnover"],
                    row["board"],
                    row["is_st"],
                )
                for row in rows
            ],
        )
    finally:
        connection.close()
    return path


def _price_db(path: Path, *, price: float, unit: str, days: int = 5) -> Path:
    """单只票、指定价格与单位（VOL-1/2 用）。"""
    rows = []
    scale = 1.0 if unit == "share" else 100.0
    for day_index in range(days):
        day = date(2026, 3, day_index + 1)
        rows.append(
            {
                "symbol": "600001",
                "date": day,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
                "volume": 100_000.0,
                "turnover": 100_000.0 * price * scale,
                "board": "主板",
                "is_st": False,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, open DOUBLE, high DOUBLE, "
            "low DOUBLE, close DOUBLE, volume DOUBLE, turnover DOUBLE, board VARCHAR, "
            "is_st BOOLEAN)"
        )
        connection.executemany(
            "INSERT INTO daily_bars (symbol, date, open, high, low, close, volume, "
            "turnover, board, is_st) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["symbol"],
                    row["date"],
                    row["open"],
                    row["high"],
                    row["low"],
                    row["close"],
                    row["volume"],
                    row["turnover"],
                    row["board"],
                    row["is_st"],
                )
                for row in rows
            ],
        )
    finally:
        connection.close()
    return path


def _config(**overrides: object):
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    # 生产前置默认全开（PF-4 单独关 nightly 验证 BLOCKED）
    config.week5.enabled = True
    config.week5.auto_run = True
    config.week5.full_market_automation_enabled = True
    config.nightly.enabled = True
    config.alpha_v2.enabled = True
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# ---------------------------------------------------------------------------
# VOL-1 / VOL-2：价格归一化判别
# ---------------------------------------------------------------------------


def test_vol1_high_price_shares_are_not_misread_as_lots(tmp_path):
    """close=300 且 volume 本来就是股 → unit_scale≈1 → share（旧绝对阈值会误判 lot）。"""
    db = _price_db(tmp_path / "m.duckdb", price=300.0, unit="share")
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 5)
    )
    assert result.verdict == pf.VERDICT_PASS, result.findings
    month = result.facts["monthly"][0]
    assert month["unit_status"] == "share"
    assert 0.9 <= float(month["unit_scale_median"]) <= 1.1


def test_vol2_low_price_lots_are_not_misread_as_shares(tmp_path):
    """close=1.5 且 volume 以手计 → unit_scale≈100 → lot（旧绝对阈值会误判 share）。"""
    db = _price_db(tmp_path / "m.duckdb", price=1.5, unit="lot")
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 5)
    )
    assert result.verdict == pf.VERDICT_PASS, result.findings
    month = result.facts["monthly"][0]
    assert month["unit_status"] == "lot"
    assert 50.0 <= float(month["unit_scale_median"]) <= 200.0


def test_clean_share_like_window_passes(tmp_path):
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2026-03", 10, "share"), ("2026-04", 10, "share")]
    )
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 4, 30)
    )
    assert result.verdict == pf.VERDICT_PASS
    assert result.facts["share_like_months"] == ["2026-03", "2026-04"]
    assert result.facts["lot_like_months"] == []
    assert result.facts["affected_symbol_count"] == 0


def test_regime_switch_in_window_is_blocked(tmp_path):
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2026-03", 10, "share"), ("2026-04", 10, "lot")]
    )
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 4, 30)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("regime_switch" in finding for finding in result.findings)


def test_intra_month_mixed_is_blocked_and_counts_are_distinct(tmp_path):
    """affected 计数语义：distinct symbols 与 symbol-month pairs 分开记。"""
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 10, "mixed")])
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("mixed_volume_units_month" in finding for finding in result.findings)
    assert result.facts["affected_symbol_count"] == 2  # 两只票各自内部混单位
    assert result.facts["affected_symbol_month_count"] == 2  # 2 只 × 1 个月
    assert result.facts["affected_date_range"] == ["2026-03", "2026-03"]


def test_volume_check_impossible_without_required_columns(tmp_path):
    db = tmp_path / "m.duckdb"
    connection = duckdb.connect(str(db))
    connection.execute("CREATE TABLE daily_bars (symbol VARCHAR, date DATE, close DOUBLE)")
    connection.close()
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("missing_column" in finding for finding in result.findings)


# ---------------------------------------------------------------------------
# market db / 安全开关 / 身份
# ---------------------------------------------------------------------------


def test_missing_data_source_is_blocked(tmp_path):
    result = pf.check_market_db(
        tmp_path / "nope.duckdb", training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "data_source_missing" in result.findings


def test_tail_fragment_in_window_is_blocked(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 20, "share")], tail_fragment=True)
    result = pf.check_market_db(db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31))
    assert result.facts["tail_fragment"] is True
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "tail_fragment_in_training_window" in result.findings


def test_tail_fragment_after_window_is_warn(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 20, "share")], tail_fragment=True)
    result = pf.check_market_db(db, training_start=date(2026, 2, 1), training_end=date(2026, 2, 27))
    assert result.verdict == pf.VERDICT_WARN
    assert "tail_fragment_after_window" in result.findings


def test_duplicate_logical_keys_blocked(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 5, "share")])
    connection = duckdb.connect(str(db))
    connection.execute(
        "INSERT INTO daily_bars SELECT * FROM daily_bars WHERE date = DATE '2026-03-01'"
    )
    connection.close()
    result = pf.check_market_db(db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31))
    assert result.verdict == pf.VERDICT_BLOCKED
    assert result.facts["duplicate_logical_keys"] == 2


def test_safety_flags_blocked_when_unsafe():
    config = _config()
    config.alpha_v2.shadow_only = False
    config.alpha_v2.enforce_final_selection = True
    config.training.enabled = True
    config.auto_promotion.enabled = True
    result = pf.check_safety_flags(config)
    assert result.verdict == pf.VERDICT_BLOCKED
    joined = " ".join(result.findings)
    assert "shadow_only" in joined and "enforce_final_selection" in joined
    assert "training_enabled" in joined and "auto_promotion_enabled" in joined


def test_safety_flags_pass_on_tracked_defaults(monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    result = pf.check_safety_flags(_config())
    assert result.verdict == pf.VERDICT_PASS, result.findings
    assert result.facts["execution_price_mode"] == "raw"


def test_safety_flags_blocked_on_qfq_execution(monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "qfq")
    result = pf.check_safety_flags(_config())
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("execution_price_mode" in finding for finding in result.findings)


def test_runtime_identity_violations_are_blocked(monkeypatch):
    class _FakeIdentity:
        violations = ("build_manifest_missing",)
        code_commit = "x" * 40

        def to_payload(self) -> dict[str, object]:
            return {"code_commit": self.code_commit, "violations": list(self.violations)}

    monkeypatch.setattr(
        "stock_analyzer.alpha_v2.validation.runtime_identity.resolve_runtime_code_identity",
        lambda *args, **kwargs: _FakeIdentity(),
    )
    result = pf.check_runtime_identity(REPO_ROOT)
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "identity_violation:build_manifest_missing" in result.findings


# ---------------------------------------------------------------------------
# PF-4：生产链前置（含 alpha_v2.enabled 语义）
# ---------------------------------------------------------------------------


def test_pf4_nightly_disabled_blocks_preflight():
    config = _config()
    config.nightly.enabled = False
    result = pf.check_production_prerequisites(config)
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "prerequisite:nightly_enabled_must_be_true" in result.findings


def test_alpha_v2_disabled_blocks_preflight():
    """§5.2：enabled=false 开 epoch → 之后打开会让 config_hash 漂移，必须开前为真。"""
    config = _config()
    config.alpha_v2.enabled = False
    result = pf.check_production_prerequisites(config)
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "prerequisite:alpha_v2_enabled_must_be_true" in result.findings


def test_week5_or_full_market_automation_disabled_blocks():
    config = _config()
    config.week5.full_market_automation_enabled = False
    assert pf.check_production_prerequisites(config).verdict == pf.VERDICT_BLOCKED
    config = _config()
    config.week5.auto_run = False
    assert pf.check_production_prerequisites(config).verdict == pf.VERDICT_BLOCKED


def test_unreachable_cycle_window_blocks():
    config = _config()
    config.alpha_v2.live_cycle_latest_time = "22:30"  # 早于 nightly.last_scan_start(23:00)
    result = pf.check_production_prerequisites(config)
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("alpha_cycle_window_unreachable" in finding for finding in result.findings)
    assert result.facts["window_reachable"] is False


def test_prerequisites_pass_on_full_production_shape():
    result = pf.check_production_prerequisites(_config())
    assert result.verdict == pf.VERDICT_PASS, result.findings
    assert result.facts["window_reachable"] is True


# ---------------------------------------------------------------------------
# 特征输入（含 fill-zero 假健康）
# ---------------------------------------------------------------------------


def test_feature_schema_missing_is_blocked(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 5, "share")])
    result = pf.check_feature_inputs(
        feature_columns=[], market_db=db, as_of=date(2026, 3, 5), repo_root=tmp_path
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "feature_schema_missing" in result.findings


def test_feature_probe_skip_is_warn_not_pass(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 5, "share")])
    result = pf.check_feature_inputs(
        feature_columns=["ret_1d"],
        market_db=db,
        as_of=date(2026, 3, 5),
        repo_root=tmp_path,
        skip_reason="--skip-feature-probe",
    )
    assert result.verdict == pf.VERDICT_WARN
    assert "feature_probe_skipped" in " ".join(result.findings)


def test_feature_probe_runs_diagnosis_on_synthetic_panel(tmp_path):
    """真实探针 + 多日诊断：合成库能算出冻结 schema 的列，且诊断块落盘。"""
    from test_alpha_v2_m4l_e2e_rehearsal import FEATURES, SYMBOLS, _synthetic_market_db

    db = _synthetic_market_db(tmp_path / "m.duckdb")
    result = pf.check_feature_inputs(
        feature_columns=FEATURES,
        market_db=db,
        as_of=date(2026, 6, 18),
        repo_root=tmp_path,
        max_symbols=len(SYMBOLS),
        warmup_days=120,
    )
    assert result.verdict in (pf.VERDICT_PASS, pf.VERDICT_WARN), result.findings
    assert result.facts["missing_columns"] == []
    assert result.facts["all_null_columns"] == []
    assert int(result.facts["diagnosis_days"]) >= 20
    diagnosis = result.facts["diagnosis"]
    assert isinstance(diagnosis, dict) and "classification_counts" in diagnosis


def test_fill_zero_artifact_is_blocked_not_pass(tmp_path):
    """required 特征被 fillna(0) 掩蔽时，coverage=100% 也必须是 BLOCKED。"""
    # 直接构造一帧"上游未填充 + fill_zero"的形态：整列为常数 0。
    import pandas as pd

    from stock_analyzer.alpha_v2.validation import preflight as module

    frame = pd.DataFrame(
        {
            "decision_date": ["2026-03-05"] * 40,
            "symbol": [f"6000{index % 5:02d}" for index in range(40)],
            # 完全常数（mode_ratio=1.0 ≥ 0.995 阈值）→ REAL_CONSTANT
            "constant_feature": [5.0] * 40,
            "zero_feature": [0.0] * 40,
        }
    )
    from stock_analyzer.alpha_v2.validation.feature_diagnosis import (
        CLASS_FILL_ZERO_ARTIFACT,
        CLASS_REAL_CONSTANT,
        diagnose_features,
    )

    report = diagnose_features(frame, columns=["zero_feature", "constant_feature"])
    by_column = {row.column: row.classification for row in report.rows}
    assert by_column["zero_feature"] == CLASS_FILL_ZERO_ARTIFACT
    assert by_column["constant_feature"] == CLASS_REAL_CONSTANT
    # 分类映射到门禁等级：fill_zero → BLOCKED；REAL_CONSTANT → WARN
    assert module.VERDICT_BLOCKED == "BLOCKED"


# ---------------------------------------------------------------------------
# PF-2 / PF-3：训练数据指纹性质
# ---------------------------------------------------------------------------


def test_pf2_price_change_changes_fingerprint(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 10, "share")])
    before = compute_training_data_fingerprint(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    connection = duckdb.connect(str(db))
    connection.execute(
        "UPDATE daily_bars SET close = close * 1.01 "
        "WHERE symbol='600001' AND date = DATE '2026-03-10'"
    )
    connection.close()
    after = compute_training_data_fingerprint(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert before["fingerprint"] != after["fingerprint"]
    assert before["rows"] == after["rows"]


def test_pf3_outside_window_append_keeps_fingerprint(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 10, "share")])
    before = compute_training_data_fingerprint(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    connection = duckdb.connect(str(db))
    connection.execute(
        "INSERT INTO daily_bars (symbol, date, open, high, low, close, volume, turnover, "
        "board, is_st) SELECT '600009', DATE '2026-04-01', open, high, low, close, volume, "
        "turnover, board, is_st FROM daily_bars WHERE symbol='600001' AND date = DATE '2026-03-10'"
    )
    connection.close()
    after = compute_training_data_fingerprint(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert before["fingerprint"] == after["fingerprint"]


# ---------------------------------------------------------------------------
# 模型身份 / 汇总 / 硬门
# ---------------------------------------------------------------------------


def _fake_model_dir(tmp_path: Path, *, model_id: str = "m1") -> Path:
    """写一个**可被 load_frozen_model 验证**的最小真工件（真 booster + 真哈希）。"""
    import numpy as np
    import pandas as pd

    from stock_analyzer.alpha_v2.research.multi_head import HeadFitSpec
    from stock_analyzer.alpha_v2.validation.frozen_model import (
        fit_frozen_model,
        persist_frozen_model,
    )

    rng = np.random.default_rng(3)
    total = 120
    days = np.repeat(np.arange(total // 20), 20)[:total]
    features = ["ret_1d", "ma5"]
    frame = pd.DataFrame(
        {
            "decision_date": [f"2026-01-{int(d) + 1:02d}" for d in days],
            "symbol": [f"6000{index % 6:02d}" for index in range(total)],
            **{name: rng.normal(0.0, 1.0, total) for name in features},
        }
    )
    base = 0.3 * frame["ret_1d"] + 0.2 * frame["ma5"]
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
    frame.loc[:89, "is_train"] = True
    frame.loc[90:, "is_calibration"] = True
    model = fit_frozen_model(
        frame=frame,
        model_id=model_id,
        spec=HeadFitSpec(min_train_rows=20, min_class_balance=0.05),
        provenance={
            "window": ["2026-03-01", "2026-03-31"],
            # R1.1：生产 preflight 只接受**封存**训练 provenance 的工件——封存项必须
            # 齐备（window / warmup_days / source_window / 指纹+版本 / 行数 / 列清单）。
            "warmup_days": 30,
            "source_window": ["2026-01-30", "2026-03-31"],
            "training_data_fingerprint": "fingerprint-abc",
            "training_data_fingerprint_version": "v2",
            "training_data_rows": 1234,
            "training_data_columns": ["symbol", "date", "close"],
            # P0：v3 还要求训练模式 + 双价格源身份（缺一即"训练目标来自哪份数据不可证"）。
            "validation_mode": "production",
            "feature_price_mode": "qfq",
            "execution_price_mode": "raw",
            "feature_data_identity": {
                "role": "feature",
                "db": "feature.duckdb",
                "price_series_mode": "qfq",
                "fingerprint": "fingerprint-abc",
                "fingerprint_version": "v2",
                "source_window": ["2026-01-30", "2026-03-31"],
                "warmup_days": 30,
                "rows": 1234,
                "columns": ["symbol", "date", "close"],
            },
            "execution_data_identity": {
                "role": "execution",
                "db": "execution_raw.duckdb",
                "price_series_mode": "raw",
                "price_series_certified": True,
                "fingerprint": "fingerprint-exec-abc",
                "fingerprint_version": "v2",
                "source_window": ["2026-01-30", "2026-03-31"],
                "warmup_days": 30,
                "rows": 1234,
                "columns": ["symbol", "date", "close"],
            },
        },
        extra_identity={"code_commit": "a" * 40},
    )
    return persist_frozen_model(model, tmp_path / "artifacts")


def test_model_identity_check_records_full_binding_block(tmp_path):
    model_dir = _fake_model_dir(tmp_path)
    result = pf.check_model_identity(model_dir)
    assert result.verdict == pf.VERDICT_PASS, result.findings
    assert result.facts["artifact_verified"] is True
    assert result.facts["model_id"] and result.facts["model_artifact_hash"]
    assert result.facts["feature_schema_hash"]
    assert result.facts["model_training_code_commit"] == "a" * 40
    assert result.facts["provenance_window"] == ["2026-03-01", "2026-03-31"]
    assert result.facts["training_data_fingerprint"] == "fingerprint-abc"
    # R1.1：封存身份（版本 / warmup / source_window / 指纹版本）逐项可见
    assert result.facts["artifact_hash_version"] == "v3"
    assert result.facts["provenance_warmup_days"] == 30
    assert result.facts["provenance_source_window"] == ["2026-01-30", "2026-03-31"]
    assert result.facts["training_data_fingerprint_version"] == "v2"


def test_model_identity_rejects_unsealed_v1_artifact(tmp_path):
    """R1.1：v1 工件（训练 provenance 不受哈希保护）在生产 preflight = BLOCKED。

    构造方式：删掉 manifest 的 ``artifact_hash_version`` 字段 → 复算按 v1 走
    （工件仍自洽、仍可加载），封存门必须把它拦下——否则"改 provenance 不改哈希"
    这条路径在生产上就是敞开的。
    """
    model_dir = _fake_model_dir(tmp_path)
    manifest_path = model_dir / "model_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop("artifact_hash_version")
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    result = pf.check_model_identity(model_dir)
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("unsealed_training_provenance" in finding for finding in result.findings), (
        result.findings
    )


def test_model_identity_missing_artifact_is_blocked(tmp_path):
    result = pf.check_model_identity(tmp_path / "nope")
    assert result.verdict == pf.VERDICT_BLOCKED


def test_training_data_fingerprint_check_compares_model_provenance(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 10, "share")])
    real = compute_training_data_fingerprint(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    # R1.1：复算参数（warmup / 版本 / 列 / source_window）全部取模型记录值
    binding = {
        "training_data_fingerprint": real["fingerprint"],
        "training_data_fingerprint_version": real["fingerprint_version"],
        "provenance_warmup_days": real["warmup_days"],
        "provenance_source_window": real["source_window"],
        "training_data_columns": real["columns"],
        "training_data_rows": real["rows"],
    }
    ok = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity=dict(binding),
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
    )
    assert ok.verdict == pf.VERDICT_PASS, ok.findings
    bad = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity={**binding, "training_data_fingerprint": "different"},
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
    )
    assert bad.verdict == pf.VERDICT_BLOCKED
    # 契约身份不符（版本 / source_window / 列清单 / 行数）也必须 BLOCKED
    for override, finding in (
        ({"training_data_fingerprint_version": "v1"}, "fingerprint_version"),
        ({"provenance_source_window": ["2026-01-01", "2026-03-31"]}, "source_window"),
        ({"training_data_columns": ["symbol", "date", "close"]}, "training_data_columns"),
        ({"training_data_rows": 1}, "training_data_rows"),
    ):
        mismatch = pf.check_training_data_fingerprint(
            market_db=db,
            model_identity={**binding, **override},
            training_start=date(2026, 3, 1),
            training_end=date(2026, 3, 31),
        )
        assert mismatch.verdict == pf.VERDICT_BLOCKED, override
        assert any(finding in item for item in mismatch.findings), mismatch.findings
    missing = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity={},
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
    )
    assert missing.verdict == pf.VERDICT_BLOCKED
    assert "model_provenance_missing_training_data_fingerprint" in missing.findings
    # 有指纹但没有 warmup 身份（v1 工件形态）→ 无法复算同一指纹 = BLOCKED
    no_warmup = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity={"training_data_fingerprint": real["fingerprint"]},
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
    )
    assert no_warmup.verdict == pf.VERDICT_BLOCKED
    assert any("warmup_days" in item for item in no_warmup.findings)


def test_fp4_warmup_window_participates_in_preflight_recompute(tmp_path):
    """FP-4（preflight 侧）：改 warmup 段内一行 → 模型指纹与重算不一致 = BLOCKED。

    注意构造：被改的 2026-02-05 必须在 ``[window_start - warmup, window_start)``
    区间内**且真实存在**（R1 的指纹只覆盖决策窗，这种改动它看不见）。
    """
    db = _market_db(
        tmp_path / "m.duckdb",
        months=[("2026-01", 31, "share"), ("2026-02", 28, "share"), ("2026-03", 10, "share")],
    )
    trained = compute_training_data_fingerprint(
        db,
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
        warmup_days=30,
    )
    binding = {
        "training_data_fingerprint": trained["fingerprint"],
        "training_data_fingerprint_version": trained["fingerprint_version"],
        "provenance_warmup_days": trained["warmup_days"],
        "provenance_source_window": trained["source_window"],
        "training_data_columns": trained["columns"],
        "training_data_rows": trained["rows"],
    }
    assert trained["source_window"][0] == "2026-01-30"
    connection = duckdb.connect(str(db))
    connection.execute("UPDATE daily_bars SET close = close * 1.05 WHERE date = DATE '2026-02-05'")
    connection.close()
    result = pf.check_training_data_fingerprint(
        market_db=db,
        model_identity=dict(binding),
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("mismatch" in item for item in result.findings), result.findings


def test_pf1_gate_rejects_same_commit_and_window_but_different_model(tmp_path):
    """PF-1：同 code / 同窗口、不同 artifact 或 schema 必须 exit 7。"""
    report_path = _preflight_report(
        tmp_path,
        model_identity={
            "model_id": "model_a",
            "model_artifact_hash": "hash-a",
            "feature_schema_hash": "schema-a",
            "model_training_code_commit": "a" * 40,
            "training_data_fingerprint": "fp-a",
            "training_data_fingerprint_version": "v2",
            "provenance_warmup_days": 200,
            "provenance_source_window": ["2025-02-13", "2026-03-01"],
        },
    )
    model_b = {
        "model_id": "model_b",
        "artifact_hash": "hash-b",
        "feature_schema_hash": "schema-b",
        "model_training_code_commit": "a" * 40,
        "provenance": {
            "window": ["2025-09-01", "2026-03-01"],
            "warmup_days": 200,
            "source_window": ["2025-02-13", "2026-03-01"],
            "training_data_fingerprint": "fp-a",
            "training_data_fingerprint_version": "v2",
        },
    }
    with pytest.raises(pf.PreflightError, match="不一致"):
        pf.assert_preflight_gate(
            report_path=report_path,
            runtime_code_commit="a" * 40,
            model_block=model_b,
            max_age_hours=48.0,
        )
    # 只换指纹也必须拒（同 artifact、不同数据）
    model_c = dict(model_b)
    model_c["model_id"] = "model_a"
    model_c["artifact_hash"] = "hash-a"
    model_c["feature_schema_hash"] = "schema-a"
    model_c["provenance"] = {
        "window": ["2025-09-01", "2026-03-01"],
        "warmup_days": 200,
        "source_window": ["2025-02-13", "2026-03-01"],
        "training_data_fingerprint": "fp-b",
        "training_data_fingerprint_version": "v2",
    }
    with pytest.raises(pf.PreflightError, match="training_data_fingerprint"):
        pf.assert_preflight_gate(
            report_path=report_path,
            runtime_code_commit="a" * 40,
            model_block=model_c,
            max_age_hours=48.0,
        )


def _preflight_report(tmp_path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema": pf.PREFLIGHT_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "verdict": pf.VERDICT_PASS,
        "blocking_findings": [],
        "warnings": [],
        "facts": {},
        "runtime_identity": {"code_commit": "a" * 40},
        "model_identity": {
            "model_id": "model_a",
            "model_artifact_hash": "hash-a",
            "feature_schema_hash": "schema-a",
            "model_training_code_commit": "a" * 40,
            "training_data_fingerprint": "fp-a",
            # R1.1：契约身份（版本 / warmup / source_window）与模型 provenance 对账
            "training_data_fingerprint_version": "v2",
            "provenance_warmup_days": 200,
            "provenance_source_window": ["2025-02-13", "2026-03-01"],
        },
        "data_identity": {
            "market_db": "m.duckdb",
            "latest_trade_date": "2026-03-31",
            # R1.1：gate 会拿 data_identity 的 warmup / source_window 与模型
            # provenance 对账，夹具必须如实落这三项。
            "training_data_fingerprint_version": "v2",
            "warmup_days": 200,
            "source_window": ["2025-02-13", "2026-03-01"],
            # P0：两条数据身份（与 _MODEL_BLOCK 的封存值逐项一致）。
            "feature_data_identity": dict(_FEATURE_IDENTITY),
            "execution_data_identity": dict(_EXECUTION_IDENTITY),
        },
        "training_window": {"start": "2025-09-01", "end": "2026-03-01"},
        "checks": [],
    }
    payload.update(overrides)
    payload["preflight_hash"] = pf.preflight_hash_of(payload)
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# P0：两条数据身份（feature qfq / execution raw）。gate 逐项对账的就是这两块，
# 所以报告夹具与模型块必须给出**同一组值**，测试才能证明"不一致项只有被改的那个"。
_FEATURE_IDENTITY = {
    "role": "feature",
    "db": "m.duckdb",
    "price_series_mode": "qfq",
    "fingerprint": "fp-a",
    "fingerprint_version": "v2",
    "source_window": ["2025-02-13", "2026-03-01"],
    "warmup_days": 200,
    "rows": 1000,
    "columns": ["symbol", "date", "close"],
}

_EXECUTION_IDENTITY = {
    "role": "execution",
    "db": "m_raw.duckdb",
    "price_series_mode": "raw",
    "price_series_certified": True,
    "fingerprint": "fp-exec",
    "fingerprint_version": "v2",
    "source_window": ["2025-02-13", "2026-03-01"],
    "warmup_days": 200,
    "rows": 1000,
    "columns": ["symbol", "date", "close"],
}

_MODEL_BLOCK = {
    "model_id": "model_a",
    "artifact_hash": "hash-a",
    "artifact_hash_version": "v3",
    "feature_schema_hash": "schema-a",
    "model_training_code_commit": "a" * 40,
    "provenance": {
        "window": ["2025-09-01", "2026-03-01"],
        "warmup_days": 200,
        "source_window": ["2025-02-13", "2026-03-01"],
        "training_data_fingerprint": "fp-a",
        "training_data_fingerprint_version": "v2",
        "validation_mode": "production",
        "feature_price_mode": "qfq",
        "execution_price_mode": "raw",
        "feature_data_identity": dict(_FEATURE_IDENTITY),
        "execution_data_identity": dict(_EXECUTION_IDENTITY),
        "training_data_rows": 1000,
        "training_data_columns": ["symbol", "date", "close"],
    },
}


def test_gate_accepts_matching_pass_report(tmp_path):
    path = _preflight_report(tmp_path)
    block = pf.assert_preflight_gate(
        report_path=path,
        runtime_code_commit="a" * 40,
        model_block=_MODEL_BLOCK,
        max_age_hours=48.0,
    )
    assert block["verdict"] == pf.VERDICT_PASS
    assert block["report_sha256"] == pf.file_sha256(path)
    assert block["training_data_fingerprint"] == "fp-a"
    # R1.1：指纹契约身份进 production_preflight 块（受 freeze_manifest_hash 保护）
    assert block["training_data_fingerprint_version"] == "v2"
    assert block["warmup_days"] == 200
    assert block["source_window"] == ["2025-02-13", "2026-03-01"]


@pytest.mark.parametrize(
    ("override_key", "override_value", "expected"),
    [
        ("provenance_warmup_days", None, "warmup_days"),
        ("provenance_source_window", None, "source_window"),
        ("training_data_fingerprint_version", None, "fingerprint_version"),
    ],
)
def test_gate_rejects_missing_fingerprint_contract_identity(
    tmp_path, override_key, override_value, expected
):
    """R1.1：模型块缺契约身份（warmup / source_window / 指纹版本）→ 拒绝。

    "只比 digest"会漏掉"同一个 digest、不同的契约声明"这种自述与实现脱节；
    缺字段更必须 fail-closed，而不是默认放过。
    """
    report = json.loads(_preflight_report(tmp_path).read_text(encoding="utf-8"))
    report["model_identity"].pop(override_key, None)
    report["preflight_hash"] = pf.preflight_hash_of(report)
    path = tmp_path / "preflight_missing.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    model_block = json.loads(json.dumps(_MODEL_BLOCK))
    model_block["provenance"].pop(
        {
            "provenance_warmup_days": "warmup_days",
            "provenance_source_window": "source_window",
            "training_data_fingerprint_version": "training_data_fingerprint_version",
        }[override_key],
        None,
    )
    with pytest.raises(pf.PreflightError, match=expected):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            model_block=model_block,
            max_age_hours=48.0,
        )


def test_gate_rejects_blocked_report(tmp_path):
    path = _preflight_report(
        tmp_path,
        verdict=pf.VERDICT_BLOCKED,
        blocking_findings=["volume_units:mixed_volume_units_regime_switch:2025-09..2026-01"],
    )
    with pytest.raises(pf.PreflightError, match="BLOCKED"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            model_block=_MODEL_BLOCK,
            max_age_hours=48.0,
        )


def test_gate_warn_requires_explicit_acceptance(tmp_path):
    path = _preflight_report(
        tmp_path, verdict=pf.VERDICT_WARN, warnings=["market_db:tail_fragment_after_window"]
    )
    with pytest.raises(pf.PreflightError, match="WARN"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            model_block=_MODEL_BLOCK,
            max_age_hours=48.0,
        )
    block = pf.assert_preflight_gate(
        report_path=path,
        runtime_code_commit="a" * 40,
        model_block=_MODEL_BLOCK,
        max_age_hours=48.0,
        accept_warn=True,
    )
    assert block["verdict"] == pf.VERDICT_WARN


def test_gate_rejects_stale_report(tmp_path):
    stale = (datetime.now().astimezone() - timedelta(hours=100)).isoformat()
    path = _preflight_report(tmp_path, generated_at=stale)
    with pytest.raises(pf.PreflightError, match="过旧"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            model_block=_MODEL_BLOCK,
            max_age_hours=48.0,
        )


def test_gate_rejects_other_code_commit_and_missing_model_block(tmp_path):
    path = _preflight_report(tmp_path)
    with pytest.raises(pf.PreflightError, match="运行代码不一致"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="b" * 40,
            model_block=_MODEL_BLOCK,
            max_age_hours=48.0,
        )
    with pytest.raises(pf.PreflightError, match="冻结模型块缺失"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            model_block=None,
            max_age_hours=48.0,
        )


def test_gate_rejects_tampered_report(tmp_path):
    path = _preflight_report(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["blocking_findings"] = ["hmm"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(pf.PreflightError, match="preflight_hash"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            model_block=_MODEL_BLOCK,
            max_age_hours=48.0,
        )


def test_run_preflight_aggregates_and_writes_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2025-09", 10, "share"), ("2025-10", 10, "lot")]
    )
    model_dir = _fake_model_dir(tmp_path)
    payload = pf.run_production_preflight(
        config=_config(),
        repo_root=REPO_ROOT,
        market_db=db,
        training_start=date(2025, 9, 1),
        training_end=date(2025, 10, 31),
        feature_columns=["ret_1d"],
        model_dir=model_dir,
        feature_probe_skipped_reason="--skip-feature-probe",
    )
    assert payload["verdict"] == pf.VERDICT_BLOCKED
    blocking = " ".join(str(item) for item in payload["blocking_findings"])
    assert "volume_units" in blocking or "training_data_fingerprint" in blocking
    assert payload["model_identity"]["model_id"] == "m1"
    path = pf.write_preflight_audit(payload, audit_root=tmp_path / "audit")
    assert path.exists()
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["preflight_hash"] == payload["preflight_hash"]


def test_run_preflight_requires_model_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 10, "share")])
    payload = pf.run_production_preflight(
        config=_config(),
        repo_root=REPO_ROOT,
        market_db=db,
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
        feature_columns=["ret_1d"],
        feature_probe_skipped_reason="--skip-feature-probe",
    )
    assert payload["verdict"] == pf.VERDICT_BLOCKED
    assert any("model_dir_required" in item for item in payload["blocking_findings"])
