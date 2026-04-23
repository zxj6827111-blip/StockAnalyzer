from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.acceptance_artifacts import build_phase_checkpoint, write_checkpoint


def _as_object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return value


def _baseline_payload() -> dict[str, object]:
    return {
        "baseline_type": "fallback_baseline",
        "model_status": {
            "lgbm_backend": "fallback_logit",
            "xgb_backend": "fallback_logit",
        },
        "background_factor_coverage": {
            "holder_count": {"non_null_ratio": 1.0, "non_zero_ratio": 0.7},
            "block_trade_net": {"non_null_ratio": 1.0, "non_zero_ratio": 0.6},
            "financing_balance": {"non_null_ratio": 1.0, "non_zero_ratio": 0.6},
            "northbound_net": {"non_null_ratio": 1.0, "non_zero_ratio": 0.6},
        },
        "walk_forward": {"summary": {"folds": 3}},
    }


def test_build_phase_checkpoint_outputs_gate_payloads(tmp_path: Path) -> None:
    baseline_path = tmp_path / "acceptance" / "baseline_report.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(_baseline_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = build_phase_checkpoint(
        phase="A",
        baseline_report=_baseline_payload(),
        baseline_report_path=baseline_path,
    )

    assert report["phase"] == "A"
    assert report["status"] in {"pass", "hold"}
    assert len(_as_object_list(report["gates"])) >= 3


def test_write_checkpoint_persists_json(tmp_path: Path) -> None:
    checkpoint = build_phase_checkpoint(
        phase="B",
        baseline_report=_baseline_payload(),
        baseline_report_path=tmp_path / "acceptance" / "baseline_report.json",
    )
    output_path = tmp_path / "acceptance" / "checkpoint_phase_b.json"
    written = write_checkpoint(checkpoint=checkpoint, output_path=output_path)

    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["phase"] == "B"
    assert "decision" in payload
