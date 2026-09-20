"""M4-H 复算入口的 phase A / phase B 隔离回归。

背景（外部复核发现的 BLOCKER）
------------------------------
M4-H 的逐票预测按 phase 落盘：phase A 是 ``fold_001.json``，phase B 概率层是
``fold_001_b.json``。两个**只应消费 phase A** 的复算脚本曾用 ``fold_*.json`` 做
discovery —— 那会把 29 个 ``*_b.json`` 一起读进来，让 pooled / 分层样本翻倍，
报告里的数字不再可复现。

修好后：``alpha_v2_m4h_report_data`` 与 ``alpha_v2_m4h_strata`` 都必须使用
``fold_[0-9][0-9][0-9].json``，且两处规则**必须一致**。本文件守住这两点。

刻意用**不同行数与不同 symbol**构造 phase A / phase B 夹具：
- 只按行数就能发现双计数（8 vs 26 行）；
- symbol 前缀 ``A*`` 只属于 phase A、``B*`` 只属于 phase B，所以"泄漏"在内容上也可见，
  不会因为两条路径恰好行数相同而被掩盖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import alpha_v2_m4h_report_data as report_data  # noqa: E402
import alpha_v2_m4h_strata as strata  # noqa: E402

PHASE_A_ROWS = {"fold_001.json": 3, "fold_002.json": 5}
PHASE_B_ROWS = {"fold_001_b.json": 7, "fold_002_b.json": 11}


def _record(fold_id: int, index: int, symbol: str) -> dict[str, Any]:
    return {
        "protocol_id": "m4h_exp_001",
        "fold_id": fold_id,
        "decision_date": f"2024-01-{index + 1:02d}",
        "symbol": symbol,
        "rank_score": 1.0 - 0.01 * index,
        "net_return_5d": 0.001 * index,
        "excess_return_5d": 0.0005 * index,
        "mae_5d": -0.01 * index,
        "mfe_5d": 0.02 * index,
        "executable": True,
    }


def _write_predictions(root: Path) -> None:
    """phase A 用 ``A`` 前缀 symbol，phase B 用 ``B`` 前缀；行数刻意不同。"""
    directory = root / "predictions"
    directory.mkdir(parents=True, exist_ok=True)
    spec = {
        "fold_001.json": (1, PHASE_A_ROWS["fold_001.json"], "A001"),
        "fold_001_b.json": (1, PHASE_B_ROWS["fold_001_b.json"], "B001"),
        "fold_002.json": (2, PHASE_A_ROWS["fold_002.json"], "A002"),
        "fold_002_b.json": (2, PHASE_B_ROWS["fold_002_b.json"], "B002"),
    }
    for name, (fold_id, rows, symbol) in spec.items():
        payload = {
            "schema": "alpha_v2_m4h_fold.v1",
            "protocol_id": "m4h_exp_001",
            "fold_id": fold_id,
            "phase": "b" if name.endswith("_b.json") else "a",
            "rows": rows,
            "records": [_record(fold_id, index, symbol) for index in range(rows)],
        }
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def predictions_root(tmp_path: Path) -> Path:
    _write_predictions(tmp_path)
    return tmp_path


def test_fixture_really_contains_phase_b_files(predictions_root: Path) -> None:
    """反向守卫：夹具必须真的含 phase B 文件、且宽 glob 会匹配到它们。

    少了这一步，即使 discovery 被改回 ``fold_*.json``，下面几条断言也可能因为
    夹具里根本没有 ``*_b.json`` 而"通过"——那就是没有牙齿的测试。
    """
    names = sorted(path.name for path in (predictions_root / "predictions").iterdir())
    assert names == [
        "fold_001.json",
        "fold_001_b.json",
        "fold_002.json",
        "fold_002_b.json",
    ]
    broad = sorted(
        path.name for path in (predictions_root / "predictions").glob("fold_*.json")
    )
    assert broad == names  # 宽 glob 会全部匹配 —— 正是要防的那个行为
    assert len(broad) == 4


def test_phase_a_discovery_excludes_phase_b(predictions_root: Path) -> None:
    """两个脚本的 phase A discovery 必须一致，且都不含任何 ``_b.json``。"""
    report_paths = report_data.phase_a_prediction_paths(predictions_root)
    strata_paths = strata.phase_a_prediction_paths(predictions_root)

    assert [path.name for path in report_paths] == ["fold_001.json", "fold_002.json"]
    # 两处规则必须一致（否则又一次"改一处漏一处"）
    assert [path.name for path in strata_paths] == [path.name for path in report_paths]
    assert not any(path.name.endswith("_b.json") for path in report_paths)
    assert not any(path.name.endswith("_b.json") for path in strata_paths)


def test_strata_loader_reads_phase_a_rows_only(predictions_root: Path) -> None:
    """行数与内容双重断言：既不多读 phase B，也不少读 phase A。"""
    frame = strata.load_predictions(predictions_root)
    expected_rows = sum(PHASE_A_ROWS.values())
    leaked_rows = expected_rows + sum(PHASE_B_ROWS.values())

    assert len(frame) == expected_rows, (
        f"strata.load_predictions 读到 {len(frame)} 行；phase A 只有 {expected_rows} 行"
        f"（若为 {leaked_rows} 行则说明 phase B 被一起读入）"
    )
    symbols = set(frame["symbol"].astype(str))
    assert symbols == {"A001", "A002"}
    assert not any(symbol.startswith("B") for symbol in symbols)


def test_report_data_loader_reads_phase_a_rows_only(predictions_root: Path) -> None:
    frame = report_data.load_pooled_predictions(predictions_root)
    expected_rows = sum(PHASE_A_ROWS.values())
    leaked_rows = expected_rows + sum(PHASE_B_ROWS.values())

    assert len(frame) == expected_rows, (
        f"report_data.load_pooled_predictions 读到 {len(frame)} 行；"
        f"phase A 只有 {expected_rows} 行（若为 {leaked_rows} 行则是双计数）"
    )
    symbols = set(frame["symbol"].astype(str))
    assert symbols == {"A001", "A002"}


def test_no_m4h_script_uses_the_broad_fold_glob() -> None:
    """源码级守卫：任何 M4-H 脚本都不得再出现 ``glob("fold_*.json")``。"""
    offenders: list[str] = []
    for path in sorted(SCRIPTS_ROOT.glob("alpha_v2_m4h_*.py")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if 'glob("fold_*.json")' in line or "glob('fold_*.json')" in line:
                offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, "仍有 M4-H 脚本使用会匹配 phase B 的宽 glob：" + "; ".join(
        offenders
    )
