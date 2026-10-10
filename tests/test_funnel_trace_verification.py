"""漏斗留档的**读侧**自检（发布清单 R10：每层计数自洽要可查，而不是只在写时成立）。

留档是影子验证唯一的证据来源，会被人编辑、被截断写、被换目录复制。写侧的
``StageTrace.__post_init__`` 只能保证落盘那一刻没撒谎，所以读侧必须自己再判一次：
计数不闭合、契约摘要不是当前契约、内容与存储摘要对不上——三种都要点名，
不能默认放行后拿它去支撑诊断结论。
"""

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import date
from pathlib import Path

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    FUNNEL_LAYERS,
    TrendStrategyContract,
)
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    build_funnel_trace,
    read_trace,
    record_stage,
    verify_trace,
    write_trace,
)

CONTRACT = DEFAULT_TREND_CONTRACT
DAY = date(2026, 10, 9)


def _trace():
    symbols = [f"6000{i}.SH" for i in range(5)]
    return build_funnel_trace(
        trade_date=DAY,
        stages=[record_stage(
            stage="hard_eligibility", kind=KIND_HARD_GATE,
            input_symbols=symbols, advanced_symbols=symbols[:3],
            rejected={"low_liquidity": symbols[3:]},
            data_as_of="2026-10-09T14:45:00", contract=CONTRACT,
        )],
        contract=CONTRACT,
    )


def _written(tmp_path: Path) -> dict:
    return read_trace(write_trace(_trace(), tmp_path, contract=CONTRACT))


def test_clean_trace_verifies_empty(tmp_path: Path) -> None:
    assert verify_trace(_written(tmp_path)) == ()


def test_count_that_lies_is_named_with_its_stage(tmp_path: Path) -> None:
    payload = _written(tmp_path)
    # 把淘汰数改小：计数不再闭合（inputs=5 != advanced=3 + rejected=1）。
    tampered = copy.deepcopy(payload)
    tampered["stages"][0]["rejected"] = {"low_liquidity": 1}
    failures = verify_trace(tampered)
    assert len(failures) == 1
    assert failures[0].startswith("trace_stage_inconsistent:0:")
    assert "do not add up" in failures[0]


def test_content_change_that_keeps_counts_still_fails_the_digest(tmp_path: Path) -> None:
    payload = _written(tmp_path)
    # 只换被留下的符号（计数照样 3 晋级 / 2 淘汰），恒等式查不出来，摘要能。
    tampered = copy.deepcopy(payload)
    tampered["stages"][0]["advanced_symbols"] = ["000001.SZ", "000002.SZ", "000004.SZ"]
    assert "trace_digest_mismatch" in verify_trace(tampered)

    notes = copy.deepcopy(payload)
    notes["stages"][0]["notes"] = "事后补的一句话"
    assert "trace_digest_mismatch" in verify_trace(notes)


def test_evidence_written_under_another_contract_is_not_trusted(tmp_path: Path) -> None:
    payload = _written(tmp_path)
    tampered = copy.deepcopy(payload)
    tampered["contract_digest"] = "0" * 16
    # 只报契约不一致这一条：重算是按**在服契约**做的，摘要对不上时先说清是口径不同，
    # 而不是甩一串跟着错的数字。
    assert verify_trace(tampered) == ("trace_contract_digest_mismatch",)
    other = TrendStrategyContract(holding_days=10)
    assert verify_trace(payload, contract=other)[0] == "trace_contract_digest_mismatch"


def test_missing_required_field_is_reported_not_defaulted(tmp_path: Path) -> None:
    payload = _written(tmp_path)
    tampered = copy.deepcopy(payload)
    del tampered["stages"][0]["data_as_of"]
    failures = verify_trace(tampered)
    assert len(failures) == 1
    assert failures[0].startswith("trace_stage_inconsistent:0:")
    assert "data_as_of" in failures[0]


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "audit_selection_funnel_verify_cli",
        Path(__file__).resolve().parents[1] / "scripts" / "audit_selection_funnel.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_refuses_to_diagnose_on_untrustworthy_evidence(tmp_path: Path) -> None:
    cli = _load_cli()
    traces_dir = tmp_path / "shadow"
    traces_dir.mkdir()
    path = write_trace(_trace(), traces_dir, contract=CONTRACT)
    assert cli.main(["--tail-dir", str(traces_dir), "--quiet"]) == 3   # 层缺记录，但证据可信

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stages"][0]["advanced"] = 1                               # 人工改计数
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["--tail-dir", str(traces_dir), "--quiet"]) == 5   # 不自洽就别下结论


def test_declared_layers_are_theones_the_verifier_rebuilds(tmp_path: Path) -> None:
    """留档里的层名必须仍在声明的九层内：改名（例如 deep_9000）要被读侧拒绝。"""
    payload = _written(tmp_path)
    tampered = copy.deepcopy(payload)
    tampered["stages"][0]["stage"] = "deep_9000"
    assert "unknown funnel stage" in verify_trace(tampered)[0]
    assert "hard_eligibility" in FUNNEL_LAYERS
