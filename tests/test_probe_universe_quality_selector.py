"""Tests for the NAS universe quality selector probe (scripts/probe_universe_quality_selector.py).

Covers the acceptance gate: full PASS, degraded fallback, insufficient
coverage/selection, NaN/Inf scores, wrong batch-source identity, minimum
input universe size, selector elapsed-time SLA, and CLI NO-GO exit code (1)
so a NO-GO is never mistaken for PASS.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_PROBE_PATH = ROOT / "scripts" / "probe_universe_quality_selector.py"


def _load_probe() -> object:
    module_name = "probe_universe_quality_selector"
    spec = importlib.util.spec_from_file_location(module_name, _PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe() -> object:
    return _load_probe()


def _pass_payload() -> dict[str, object]:
    return {
        "configured_primary": "vendor_zip_overlay",
        "provider_class": ("stock_analyzer.data.vendor_zip_overlay.VendorZipOverlayProvider"),
        "batch_source_module": "stock_analyzer.data.vendor_zip_overlay",
        "selector_mode": "quality",
        "fallback_reason": "",
        "input_count": 5000,
        "batch_coverage_ratio": 0.95,
        "batch_calls": 1,
        "selected_count": 300,
        "score_has_nan_inf": False,
    }


def test_acceptance_pass(probe: object) -> None:
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        _pass_payload(), min_coverage=0.90, target_size=300
    )
    assert failures == []


def test_degraded_fallback_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["selector_mode"] = "degraded_fallback"
    payload["fallback_reason"] = "batch_metrics_unavailable"
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("selector_mode" in failure for failure in failures)
    assert any("fallback_reason" in failure for failure in failures)


def test_coverage_below_minimum_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["batch_coverage_ratio"] = 0.50
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("batch_coverage_ratio" in failure for failure in failures)


def test_selected_below_target_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["selected_count"] = 10
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("selected_count" in failure for failure in failures)


def test_nan_inf_scores_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["score_has_nan_inf"] = True
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("score_has_nan_inf" in failure for failure in failures)


def test_score_has_nan_inf_detects_nan_score(probe: object) -> None:
    report = {
        "selected": [
            {"symbol": "000001", "score": math.nan, "components": {"turnover": 1.0}},
        ]
    }
    assert probe._score_has_nan_inf(report) is True  # type: ignore[attr-defined]


def test_score_has_nan_inf_detects_inf_component(probe: object) -> None:
    report = {
        "selected": [
            {
                "symbol": "000001",
                "score": 0.5,
                "components": {"turnover": math.inf},
            },
        ]
    }
    assert probe._score_has_nan_inf(report) is True  # type: ignore[attr-defined]


def test_score_has_nan_inf_clean(probe: object) -> None:
    report = {
        "selected": [
            {"symbol": "000001", "score": 0.5, "components": {"turnover": 1.0}},
        ]
    }
    assert probe._score_has_nan_inf(report) is False  # type: ignore[attr-defined]


def test_wrong_batch_source_module_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["batch_source_module"] = "stock_analyzer.data.market_warehouse"
    payload["provider_class"] = "stock_analyzer.data.market_warehouse.MarketWarehouse"
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("batch_source_module" in failure for failure in failures)
    assert any("provider_class" in failure for failure in failures)


def test_wrong_configured_primary_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["configured_primary"] = "market_warehouse"
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("configured_primary" in failure for failure in failures)


def test_primary_alias_normalization(probe: object) -> None:
    assert probe._normalize_primary("vendor_overlay") == "vendor_zip_overlay"  # type: ignore[attr-defined]
    assert probe._normalize_primary("LOCAL_VENDOR_ZIP") == "vendor_zip_overlay"  # type: ignore[attr-defined]
    assert probe._normalize_primary("market_warehouse") == "market_warehouse"  # type: ignore[attr-defined]


def test_input_below_minimum_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["input_count"] = 3000
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300, min_input_count=5000
    )
    assert any("min_input_count" in failure for failure in failures)


def test_input_at_threshold_passes(probe: object) -> None:
    payload = _pass_payload()
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300, min_input_count=5000
    )
    assert failures == []


def test_elapsed_below_max_passes(probe: object) -> None:
    payload = _pass_payload()
    payload["elapsed_ms"] = 120000
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300, max_elapsed_ms=300000
    )
    assert failures == []


def test_elapsed_above_max_rejected(probe: object) -> None:
    payload = _pass_payload()
    payload["elapsed_ms"] = 600000
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300, max_elapsed_ms=300000
    )
    assert any("max_elapsed_ms" in failure for failure in failures)


def test_default_sla_rejects_81000ms_without_override(probe: object) -> None:
    payload = _pass_payload()
    payload["elapsed_ms"] = 81000
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert any("max_elapsed_ms=30000" in failure for failure in failures)


def test_default_sla_accepts_under_30s_without_override(probe: object) -> None:
    payload = _pass_payload()
    payload["elapsed_ms"] = 29000
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300
    )
    assert failures == []


def test_elapsed_check_disabled_when_zero(probe: object) -> None:
    payload = _pass_payload()
    payload["elapsed_ms"] = 999999
    failures = probe._acceptance_failures(  # type: ignore[attr-defined]
        payload, min_coverage=0.90, target_size=300, max_elapsed_ms=0
    )
    assert failures == []


def _write_synthetic_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "probe_test.yaml"
    raw_config = (ROOT / "config" / "default.yaml").read_text(encoding="utf-8")
    synthetic_config = raw_config.replace("primary: market_warehouse", "primary: synthetic_test", 1)
    config_path.write_text(synthetic_config, encoding="utf-8")
    return config_path


def _clear_sa_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the data-source primary so the repo .env (loaded by config.py via
    load_dotenv) cannot silently override the synthetic test config."""
    monkeypatch.setenv("SA__DATA_SOURCE__PRIMARY", "synthetic_test")


def test_cli_no_go_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_sa_overrides(monkeypatch)
    config_path = _write_synthetic_config(tmp_path)
    probe = _load_probe()
    exit_code = probe._main(["--config", str(config_path), "--target-size", "300"])  # type: ignore[attr-defined]
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload.get("acceptance_failures") or payload.get("error")


def test_cli_min_input_count_arg_is_enforced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_sa_overrides(monkeypatch)
    config_path = _write_synthetic_config(tmp_path)
    probe = _load_probe()
    exit_code = probe._main(  # type: ignore[attr-defined]
        ["--config", str(config_path), "--target-size", "300", "--min-input-count", "99999999"]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any("min_input_count" in failure for failure in payload.get("acceptance_failures", []))


def test_cli_max_elapsed_ms_arg_is_enforced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_sa_overrides(monkeypatch)
    config_path = _write_synthetic_config(tmp_path)
    probe = _load_probe()
    calls = {"n": 0}

    def _fake_perf_counter() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 2.0

    monkeypatch.setattr("time.perf_counter", _fake_perf_counter)
    exit_code = probe._main(  # type: ignore[attr-defined]
        ["--config", str(config_path), "--target-size", "300", "--max-elapsed-ms", "1"]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["elapsed_ms"] == 2000
    assert any("max_elapsed_ms" in failure for failure in payload.get("acceptance_failures", []))
