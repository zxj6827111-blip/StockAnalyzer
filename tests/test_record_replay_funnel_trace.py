"""漏斗前两层留档的接线（改进计划 §2「全市场 → 硬性资格检查」）。

生产选择器只有逐原因计数，所以历史留档由重放 sidecar 供符号级事实。这里钉的是：
落下来的留档**必须可复核**，而供不出事实的日子必须变成读得到的 not_emitted，
不是悄悄少两层，也不是补一条看起来完整的留档。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "record_replay_funnel_trace", REPO_ROOT / "scripts" / "record_replay_funnel_trace.py"
)
recorder = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(recorder)


def _fact(day: str, *, eligible: list[str], active: list[str],
          reasons: dict[str, str]) -> dict:
    return {
        "decision_date": day,
        "as_of": day,
        "universe_snapshot_id": f"replay:test:{day}",
        "eligible_symbols": eligible,
        "expected_active_symbols": active,
        "known_suspended_symbols": [],
        "excluded_reasons": reasons,
        "survivorship_coverage": "incomplete_or_unknown",
        "delisting_coverage_verified": False,
    }


def _write(tmp_path: Path, facts: list[dict]) -> Path:
    path = tmp_path / "universe_facts.jsonl"
    path.write_text("".join(json.dumps(f) + "\n" for f in facts), encoding="utf-8")
    return path


def test_symbol_level_facts_become_the_first_two_layers(tmp_path: Path) -> None:
    from stock_analyzer.research.funnel_trace import verify_trace

    facts = _write(tmp_path, [
        _fact("2026-03-02",
              eligible=["600000", "000001", "300750"],
              active=["600000", "000001"],
              reasons={"300750": "min_avg_turnover_20"}),
        _fact("2026-03-03",
              eligible=["600000", "000001"],
              active=["600000"],
              reasons={"000001": "is_st"}),
    ])
    rc = recorder.main(["--universe-facts", str(facts),
                        "--out-dir", str(tmp_path / "traces"), "--quiet"])
    assert rc == recorder.RC_OK

    written = sorted((tmp_path / "traces").rglob("*.json"))
    assert len(written) == 2
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert [item["stage"] for item in payload["stages"]] == ["universe", "hard_eligibility"]
    assert verify_trace(payload) == ()

    stages = {item["stage"]: item for item in payload["stages"]}
    # 计数恒等式由 StageTrace 写侧钉死：晋级 2 + 淘汰 1 = 输入 3
    assert stages["hard_eligibility"]["inputs"] == 3
    assert stages["hard_eligibility"]["advanced"] == 2
    assert stages["hard_eligibility"]["rejected"] == {"min_avg_turnover_20": 1}
    # 幸存者偏差没被证明过，这个事实必须留在证据里而不是被数字掩盖
    assert "survivorship_coverage=incomplete_or_unknown" in stages["universe"]["notes"]
    assert "delisting_coverage_verified=False" in stages["universe"]["notes"]


def test_counts_only_day_is_reported_not_invented(tmp_path: Path) -> None:
    facts = _write(tmp_path, [
        _fact("2026-03-02", eligible=["600000"], active=["600000"], reasons={}),
        # 只有计数的旧 payload 形状：生产者拒收，脚本必须如实报"这一天没落"
        {"decision_date": "2026-03-03", "eligible_count": 5110, "rejected_count_by_reason": {}},
    ])
    rc = recorder.main(["--universe-facts", str(facts),
                        "--out-dir", str(tmp_path / "traces"), "--quiet"])
    assert rc == recorder.RC_PARTIAL
    assert len(list((tmp_path / "traces").rglob("*.json"))) == 1


def test_missing_sidecar_is_a_real_exit_code(tmp_path: Path) -> None:
    rc = recorder.main(["--universe-facts", str(tmp_path / "nope.jsonl"),
                        "--out-dir", str(tmp_path / "traces")])
    assert rc == recorder.RC_ERROR
