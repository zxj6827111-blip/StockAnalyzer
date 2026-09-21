"""M4-L §29：Production Data Preflight 专项测试（deterministic fixture）。

覆盖：干净数据 PASS / mixed volume BLOCKED / 尾段残缺分级 / qfq BLOCKED /
安全开关 BLOCKED / 身份违例 BLOCKED / 特征 schema BLOCKED / 数据源缺失 BLOCKED，
以及 freeze 硬门的三要素（新鲜度、同代码、同训练窗）与工件防篡改。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from stock_analyzer.alpha_v2.validation import preflight as pf
from stock_analyzer.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _market_db(
    path: Path,
    *,
    months: list[tuple[str, int, str]],
    tail_fragment: bool = False,
) -> Path:
    """造 daily_bars：``months`` = [(YYYY-MM, 天数, "share"|"lot"|"mixed")]。"""
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
                        "close": price,
                        "volume": volume,
                        "turnover": turnover,
                        "board": "主板",
                        "is_st": False,
                    }
                )
    if tail_fragment and rows:
        last_date = max(row["date"] for row in rows)
        rows = [row for row in rows if not (row["date"] == last_date and row["symbol"] == "600002")]
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE daily_bars (symbol VARCHAR, date DATE, close DOUBLE, "
            "volume DOUBLE, turnover DOUBLE, board VARCHAR, is_st BOOLEAN)"
        )
        connection.executemany(
            "INSERT INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["symbol"], row["date"], row["close"], row["volume"],
                    row["turnover"], row["board"], row["is_st"],
                )
                for row in rows
            ],
        )
    finally:
        connection.close()
    return path


def _config(**overrides: object):
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# ---------------------------------------------------------------------------
# volume unit gate（§22）
# ---------------------------------------------------------------------------


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


def test_regime_switch_in_window_is_blocked(tmp_path):
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2026-03", 10, "share"), ("2026-04", 10, "lot")]
    )
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 4, 30)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("regime_switch" in finding for finding in result.findings)


def test_intra_month_mixed_is_blocked(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 10, "mixed")])
    result = pf.check_volume_units(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert any("mixed_volume_units_month" in finding for finding in result.findings)


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
# market db（§21 C）
# ---------------------------------------------------------------------------


def test_missing_data_source_is_blocked(tmp_path):
    result = pf.check_market_db(
        tmp_path / "nope.duckdb",
        training_start=date(2026, 3, 1),
        training_end=date(2026, 3, 31),
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "data_source_missing" in result.findings


def test_tail_fragment_in_window_is_blocked(tmp_path):
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2026-03", 20, "share")], tail_fragment=True
    )
    result = pf.check_market_db(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert result.facts["tail_fragment"] is True
    assert result.verdict == pf.VERDICT_BLOCKED
    assert "tail_fragment_in_training_window" in result.findings


def test_tail_fragment_after_window_is_warn(tmp_path):
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2026-03", 20, "share")], tail_fragment=True
    )
    result = pf.check_market_db(
        db, training_start=date(2026, 2, 1), training_end=date(2026, 2, 27)
    )
    assert result.verdict == pf.VERDICT_WARN
    assert "tail_fragment_after_window" in result.findings


def test_duplicate_logical_keys_blocked(tmp_path):
    db = _market_db(tmp_path / "m.duckdb", months=[("2026-03", 5, "share")])
    connection = duckdb.connect(str(db))
    connection.execute(
        "INSERT INTO daily_bars SELECT * FROM daily_bars WHERE date = DATE '2026-03-01'"
    )
    connection.close()
    result = pf.check_market_db(
        db, training_start=date(2026, 3, 1), training_end=date(2026, 3, 31)
    )
    assert result.verdict == pf.VERDICT_BLOCKED
    assert result.facts["duplicate_logical_keys"] == 2


# ---------------------------------------------------------------------------
# 安全开关 / 身份（§21 A/B）
# ---------------------------------------------------------------------------


def test_safety_flags_blocked_when_unsafe():
    config = _config()
    config.alpha_v2.shadow_only = False
    config.alpha_v2.enforce_final_selection = True  # 生产语义被打开
    config.training.enabled = True
    config.auto_promotion.enabled = True
    result = pf.check_safety_flags(config)
    assert result.verdict == pf.VERDICT_BLOCKED
    joined = " ".join(result.findings)
    assert "shadow_only" in joined and "enforce_final_selection" in joined
    assert "training_enabled" in joined and "auto_promotion_enabled" in joined


def test_safety_flags_pass_on_tracked_defaults(monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    config = _config()
    result = pf.check_safety_flags(config)
    assert result.verdict == pf.VERDICT_PASS, result.findings
    assert result.facts["execution_price_mode"] == "raw"


def test_safety_flags_blocked_on_qfq_execution(monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "qfq")
    config = _config()
    result = pf.check_safety_flags(config)
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
# 特征输入（§21 D）
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


def test_feature_probe_runs_on_synthetic_panel(tmp_path):
    """真实探针：合成库 + 面板链路能算出冻结 schema 的列（E2E 演练同款数据）。"""
    from test_alpha_v2_m4l_e2e_rehearsal import (  # noqa: PLC0415
        FEATURES,
        SYMBOLS,
        _synthetic_market_db,
    )

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


# ---------------------------------------------------------------------------
# 汇总与 freeze 硬门（§24/§25）
# ---------------------------------------------------------------------------


def _report(tmp_path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema": pf.PREFLIGHT_SCHEMA,
        "generated_at": datetime.now().astimezone().isoformat(),
        "verdict": pf.VERDICT_PASS,
        "blocking_findings": [],
        "warnings": [],
        "facts": {},
        "runtime_identity": {"code_commit": "a" * 40},
        "data_identity": {"market_db": "m.duckdb", "latest_trade_date": "2026-03-31"},
        "training_window": {"start": "2025-09-01", "end": "2026-03-01"},
        "checks": [],
    }
    payload.update(overrides)
    payload["preflight_hash"] = pf.preflight_hash_of(payload)
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_gate_accepts_matching_pass_report(tmp_path):
    path = _report(tmp_path)
    block = pf.assert_preflight_gate(
        report_path=path,
        runtime_code_commit="a" * 40,
        training_window=["2025-09-01", "2026-03-01"],
        max_age_hours=48.0,
    )
    assert block["verdict"] == pf.VERDICT_PASS
    assert block["report_sha256"] == pf.file_sha256(path)


def test_gate_rejects_blocked_report(tmp_path):
    path = _report(
        tmp_path,
        verdict=pf.VERDICT_BLOCKED,
        blocking_findings=["volume_units:mixed_volume_units_regime_switch:2025-09..2026-01"],
    )
    with pytest.raises(pf.PreflightError, match="BLOCKED"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            training_window=["2025-09-01", "2026-03-01"],
            max_age_hours=48.0,
        )


def test_gate_warn_requires_explicit_acceptance(tmp_path):
    path = _report(
        tmp_path, verdict=pf.VERDICT_WARN, warnings=["market_db:tail_fragment_after_window"]
    )
    with pytest.raises(pf.PreflightError, match="WARN"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            training_window=["2025-09-01", "2026-03-01"],
            max_age_hours=48.0,
        )
    block = pf.assert_preflight_gate(
        report_path=path,
        runtime_code_commit="a" * 40,
        training_window=["2025-09-01", "2026-03-01"],
        max_age_hours=48.0,
        accept_warn=True,
    )
    assert block["verdict"] == pf.VERDICT_WARN


def test_gate_rejects_stale_report(tmp_path):
    stale = (datetime.now().astimezone() - timedelta(hours=100)).isoformat()
    path = _report(tmp_path, generated_at=stale)
    with pytest.raises(pf.PreflightError, match="过旧"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            training_window=["2025-09-01", "2026-03-01"],
            max_age_hours=48.0,
        )


def test_gate_rejects_other_code_commit_and_window(tmp_path):
    path = _report(tmp_path)
    with pytest.raises(pf.PreflightError, match="运行代码不一致"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="b" * 40,
            training_window=["2025-09-01", "2026-03-01"],
            max_age_hours=48.0,
        )
    with pytest.raises(pf.PreflightError, match="训练窗"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            training_window=["2025-01-01", "2026-03-01"],
            max_age_hours=48.0,
        )
    with pytest.raises(pf.PreflightError, match="provenance.window"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            training_window=None,
            max_age_hours=48.0,
        )


def test_gate_rejects_tampered_report(tmp_path):
    path = _report(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["verdict"] = pf.VERDICT_PASS
    raw["blocking_findings"] = ["hmm"]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(pf.PreflightError, match="preflight_hash"):
        pf.assert_preflight_gate(
            report_path=path,
            runtime_code_commit="a" * 40,
            training_window=["2025-09-01", "2026-03-01"],
            max_age_hours=48.0,
        )


def test_run_preflight_aggregates_and_writes_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("SA__EVOLUTION__EXECUTION_SPEC__PRICE_SERIES_MODE", "raw")
    db = _market_db(
        tmp_path / "m.duckdb", months=[("2025-09", 10, "share"), ("2025-10", 10, "lot")]
    )
    payload = pf.run_production_preflight(
        config=_config(),
        repo_root=REPO_ROOT,
        market_db=db,
        training_start=date(2025, 9, 1),
        training_end=date(2025, 10, 31),
        feature_columns=["ret_1d"],
        feature_probe_skipped_reason="--skip-feature-probe",
    )
    assert payload["verdict"] == pf.VERDICT_BLOCKED
    assert any("volume_units" in item for item in payload["blocking_findings"])
    path = pf.write_preflight_audit(payload, audit_root=tmp_path / "audit")
    assert path.exists()
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["preflight_hash"] == payload["preflight_hash"]
