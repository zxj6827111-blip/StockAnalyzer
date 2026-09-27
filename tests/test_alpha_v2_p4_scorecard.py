"""P4-B1 历史扫描成绩单回归。

覆盖任务书要求的三类测试：

1. **聚合正确性**：10 条手工构造的预测（含 1 条 no-fill、1 条未成熟），
   平均收益 / 胜率 / 桶分布全部与手算值逐项对照；
2. **horizon 边界**：T+20/T+60 只能作为评价指标从 outcome 读取——列存在才
   出现，列不存在必须如实标注 unavailable；整个运行不写任何输入工件
   （目录树哈希 + mtime 前后对照），因此 label/训练语义不可能被触碰；
3. **fail closed**：缺 fold 文件 / 缺 outcome / schema 不符 / join 缺行 /
   5d 数值矛盾 / executable 不一致 / 协议混杂 / 跨 fold 日期重叠 /
   输出目录位于 m4h root 内——全部以真实退出码失败且不产出半份报告。

夹具刻意复刻实测 schema 的关键事实：收益列是 object 列、
"not_available" 哨兵、matured 为布尔列、phase B 文件为 ``fold_001_b.json``。
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import alpha_v2_p4_scorecard as cli  # noqa: E402

from stock_analyzer.alpha_v2.research import reporting  # noqa: E402

PROTOCOL_ID = "m4h_test_fixture"
CACHE_KEY = "test01"

#: (decision_date, symbol, rank_score, net_return_5d, executable, matured)
ROWS: tuple[tuple[str, str, float, float | None, bool, bool], ...] = (
    ("2023-01-02", "A", 0.95, 0.10, True, True),
    ("2023-01-02", "B", 0.85, -0.05, True, True),
    ("2023-01-02", "C", 0.75, 0.02, True, True),
    ("2023-01-02", "D", 0.65, -0.10, True, True),
    ("2023-01-02", "E", 0.99, None, False, False),  # no-fill：limit_up_open
    ("2023-02-06", "A", 0.55, 0.04, True, True),
    ("2023-02-06", "B", 0.45, 0.06, True, True),
    ("2023-02-06", "C", 0.35, -0.02, True, True),
    ("2023-02-06", "F", 0.90, None, True, False),  # 未成熟
    ("2023-02-06", "G", 0.05, 0.00, True, True),
)

HORIZON_FACTOR = {3: 0.5, 5: 1.0, 10: 2.0, 15: 1.5, 20: 3.0, 60: 6.0}
ENTRY_DATE = {"2023-01-02": "2023-01-03", "2023-02-06": "2023-02-07"}

# T+5 手算期望值（总体 = 8 条 executable ∧ matured ∧ 数值收益）：
#   mean = 0.05/8 = 0.00625；median = (0.00+0.02)/2 = 0.01；
#   positive = 4/8 = 0.5（正收益：0.10/0.02/0.04/0.06）；best/worst = ±0.10。
T5_MEAN = 0.00625


def _not_available(value: float | None) -> float | str:
    return reporting.NOT_AVAILABLE if value is None else value


def _prediction_record(row: tuple[str, str, float, float | None, bool, bool]) -> dict[str, Any]:
    date, symbol, score, net5, executable, _matured = row
    return {
        "protocol_id": PROTOCOL_ID,
        "fold_id": 1,
        "decision_date": date,
        "symbol": symbol,
        "rank_score": score,
        "net_return_5d": _not_available(net5),
        "excess_return_5d": _not_available(net5 - 0.01 if net5 is not None else None),
        "mae_5d": _not_available(-0.05 if net5 is not None else None),
        "mfe_5d": _not_available(0.05 if net5 is not None else None),
        "entry_date": ENTRY_DATE[date],
        "entry_price_raw": 10.0,
        "no_fill_reason": "" if executable else "limit_up_open",
        "executable": executable,
    }


def _phase_b_record(symbol: str) -> dict[str, Any]:
    record = _prediction_record(("2023-01-02", symbol, 0.5, 0.01, True, True))
    record["fold_id"] = 1
    return record


def _outcome_frame(
    horizons: tuple[int, ...],
    *,
    drop: frozenset[tuple[str, str]] = frozenset(),
    flip_executable: tuple[str, str] | None = None,
    tamper_5d: tuple[str, str] | None = None,
    add_regime: bool = False,
) -> pd.DataFrame:
    rows = []
    for date, symbol, _score, net5, executable, matured in ROWS:
        if (date, symbol) in drop:
            continue
        entry: dict[str, Any] = {
            "decision_date": date,
            "symbol": symbol,
            "executable": (not executable) if flip_executable == (date, symbol) else executable,
            "no_fill_reason": "" if executable else "limit_up_open",
            "entry_date": ENTRY_DATE[date],
            "entry_price_raw": 10.0,
            "entry_price_net": 10.0,
            "benchmark_name": "eligible_ew",
            "price_mode": "raw",
            "price_mode_certified": True,
            "execution_uncertain": False,
        }
        if add_regime:
            entry["market_regime"] = "bull" if date == "2023-01-02" else "bear"
        for horizon in horizons:
            base = net5 * HORIZON_FACTOR[horizon] if net5 is not None else None
            if tamper_5d == (date, symbol) and horizon == 5:
                base = 0.99
            entry[f"net_return_{horizon}d"] = _not_available(base)
            entry[f"matured_{horizon}d"] = bool(matured)
            entry[f"exit_no_fill_{horizon}d"] = False
            excess = base - 0.01 if base is not None else None
            entry[f"excess_return_{horizon}d"] = _not_available(excess)
            entry[f"mae_{horizon}d"] = _not_available(-0.05 if base is not None else None)
            entry[f"mfe_{horizon}d"] = _not_available(0.05 if base is not None else None)
        rows.append(entry)
    return pd.DataFrame(rows)


def _write_root(
    root: Path,
    *,
    horizons: tuple[int, ...] = (3, 5, 10, 15),
    extra_phase_b: bool = False,
    overlapping_fold: bool = False,
    bad_schema: bool = False,
    include_manifest: bool = True,
    include_cache: bool = True,
    manifest_protocol: str = PROTOCOL_ID,
    cache_key: str = CACHE_KEY,
    manifest_cache_key: str | None = None,
    frame_kwargs: dict[str, Any] | None = None,
) -> Path:
    frame_kwargs = frame_kwargs or {}
    predictions_dir = root / "predictions"
    predictions_dir.mkdir(parents=True)
    records = [_prediction_record(row) for row in ROWS]
    payload: dict[str, Any] = {
        "schema": "something.else" if bad_schema else "alpha_v2_m4h_fold.v1",
        "protocol_id": PROTOCOL_ID,
        "fold_id": 1,
        "rows": len(records),
        "phase": "a",
        "records": records,
    }
    (predictions_dir / "fold_001.json").write_text(json.dumps(payload), encoding="utf-8")
    if extra_phase_b:
        b_records = [_phase_b_record(s) for s in ("P1", "P2", "P3")]
        (predictions_dir / "fold_001_b.json").write_text(
            json.dumps(
                {
                    "schema": "alpha_v2_m4h_fold.v1",
                    "protocol_id": PROTOCOL_ID,
                    "fold_id": 1,
                    "rows": len(b_records),
                    "phase": "b",
                    "records": b_records,
                }
            ),
            encoding="utf-8",
        )
    if overlapping_fold:
        z_record = _prediction_record(("2023-01-02", "Z1", 0.5, 0.01, True, True))
        z_record["fold_id"] = 2
        (predictions_dir / "fold_002.json").write_text(
            json.dumps(
                {
                    "schema": "alpha_v2_m4h_fold.v1",
                    "protocol_id": PROTOCOL_ID,
                    "fold_id": 2,
                    "rows": 1,
                    "phase": "a",
                    "records": [z_record],
                }
            ),
            encoding="utf-8",
        )

    (root / "audit").mkdir()
    (root / "metrics").mkdir()
    (root / "cache").mkdir()
    if include_manifest:
        (root / "audit" / "run_manifest.json").write_text(
            json.dumps(
                {
                    "schema": "alpha_v2_m4h_run.v1",
                    "protocol": {
                        "protocol_id": manifest_protocol,
                        "experiment_id": "TEST_EXP",
                        "code_commit": "abc123def",
                        "protocol_hash": "hash0000",
                        "execution_contract": {
                            "execution_price_mode": "raw",
                            "entry_mode": "next_session_open",
                        },
                    },
                    "protocol_hash": "hash0000",
                    "dataset": {
                        "dataset_cache": (f"saved:dataset_{manifest_cache_key or cache_key}.pkl")
                    },
                }
            ),
            encoding="utf-8",
        )
    if include_cache:
        with open(root / "cache" / f"outcomes_{cache_key}.pkl", "wb") as handle:
            pickle.dump(
                {
                    "frame": _outcome_frame(horizons, **frame_kwargs),
                    "diagnostics": {
                        "price_mode": "raw",
                        "price_mode_certified": True,
                        "entry_mode": "next_session_open",
                        "cost": {"round_trip_cost_rate": 0.00112},
                        "slippage_ratio": 0.0015,
                        "no_fill_by_reason": {"limit_up_open": 1},
                    },
                },
                handle,
            )
    (root / "metrics" / "metrics_summary.json").write_text(
        json.dumps(
            {
                "schema": "alpha_v2_m4h_run.v1",
                "primary_horizon": 5,
                "folds_planned": 1,
                "folds_usable": 1,
                "historical_locked_oos_mature_decision_dates": 2,
                "alpha_verified": False,
                "production_promotion": "LOCKED",
            }
        ),
        encoding="utf-8",
    )
    (root / "audit" / "leakage_audit.json").write_text(
        json.dumps(
            {
                "schema": "alpha_v2_m4h_run.v1",
                "lookahead_violations": 0,
                "pit_violations": 0,
                "execution_violations": 0,
                "calibration_violations": 0,
            }
        ),
        encoding="utf-8",
    )
    return root


def _tree_hash(root: Path) -> dict[str, tuple[str, int]]:
    snapshot: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return snapshot


def _run_cli(root: Path, out: Path, *extra: str) -> int:
    return cli.main(["--m4h-root", str(root), "--out", str(out), *extra])


# ---------------------------------------------------------------------------
# 1. 聚合正确性（手算对照）
# ---------------------------------------------------------------------------


def test_aggregation_matches_hand_computed(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h")
    scorecard, outputs = reporting.run_scorecard(root, tmp_path / "out", top_n=20)

    overall = scorecard["overall"]
    assert overall["evaluation_period"] == {"from": "2023-01-02", "to": "2023-02-06"}
    assert overall["decision_count"] == 2
    assert overall["prediction_rows"] == 10
    assert overall["symbols_count"] == 7  # A B C D E F G
    assert overall["fold_count"] == 1
    assert overall["executable_rows"] == 9
    assert overall["no_fill_rows"] == 1
    assert overall["no_fill_by_reason"] == {"limit_up_open": 1}
    assert overall["primary_horizon"] == 5
    assert overall["primary_mature_outcome_count"] == 8

    t5 = scorecard["horizon_stats"]["T+5"]
    assert t5["samples"] == 8
    assert t5["average_return"] == pytest.approx(T5_MEAN, abs=1e-9)
    assert t5["median_return"] == pytest.approx(0.01, abs=1e-9)
    assert t5["positive_rate"] == pytest.approx(0.5, abs=1e-9)
    assert t5["best_return"] == pytest.approx(0.10, abs=1e-9)
    assert t5["worst_return"] == pytest.approx(-0.10, abs=1e-9)
    assert t5["MAE"] == pytest.approx(-0.05, abs=1e-9)
    assert t5["MFE"] == pytest.approx(0.05, abs=1e-9)
    assert t5["average_excess_return"] == pytest.approx(T5_MEAN - 0.01, abs=1e-9)
    assert t5["immature_excluded"] == 1  # F：executable 但未成熟

    # 其他 horizon 均值 = 5d 均值 × factor（手算缩放关系）
    for horizon, factor in ((3, 0.5), (10, 2.0), (15, 1.5)):
        avg = scorecard["horizon_stats"][f"T+{horizon}"]["average_return"]
        assert avg == pytest.approx(T5_MEAN * factor, abs=1e-9)

    buckets = {b["score_bucket"]: b for b in scorecard["score_buckets"]["buckets"]}
    assert buckets["90-100"]["samples"] == 1
    assert buckets["90-100"]["avg_return"] == pytest.approx(0.10, abs=1e-9)
    assert buckets["80-90"]["avg_return"] == pytest.approx(-0.05, abs=1e-9)
    assert buckets["60-70"]["avg_return"] == pytest.approx(-0.10, abs=1e-9)
    assert buckets["0-10"]["avg_return"] == pytest.approx(0.00, abs=1e-9)
    assert buckets["20-30"]["samples"] == 0  # 空桶如实保留
    # 手算：相邻满桶倒挂 4 处（80-90→70-80、60-70→50-60、50-60→40-50、30-40→0-10）
    assert scorecard["score_buckets"]["monotonic_nonincreasing"] is False
    assert len(scorecard["score_buckets"]["monotonicity_violations"]) == 4
    assert scorecard["score_buckets"]["top_bottom_spread"] == pytest.approx(0.10, abs=1e-9)

    yearly = scorecard["yearly"]
    assert len(yearly) == 1
    assert yearly[0]["year"] == "2023"
    assert yearly[0]["decision_days"] == 2
    assert yearly[0]["samples"] == 8
    assert yearly[0]["avg_return"] == pytest.approx(T5_MEAN, abs=1e-9)
    assert yearly[0]["win_rate"] == pytest.approx(0.5, abs=1e-9)

    worst = scorecard["failure_analysis"]["worst_return_top"]
    assert len(worst) == 8  # top_n=20 大于样本数时如实输出全部
    assert worst[0]["symbol"] == "D"
    assert worst[0]["decision_date"] == "2023-01-02"
    assert worst[0]["score"] == pytest.approx(0.65, abs=1e-9)
    assert worst[0]["return"] == pytest.approx(-0.10, abs=1e-9)
    assert worst[0]["MAE"] == pytest.approx(-0.05, abs=1e-9)
    assert worst[0]["MFE"] == pytest.approx(0.05, abs=1e-9)
    assert worst[-1]["return"] == pytest.approx(0.10, abs=1e-9)

    assert scorecard["capability"]["outcome_horizons_present"] == [3, 5, 10, 15]
    assert set(scorecard["capability"]["horizons_unavailable"]) == {"20", "60"}
    assert "T+20" not in scorecard["horizon_stats"]
    assert scorecard["market_regime"]["available"] is False
    assert scorecard["warnings"] == []  # 完整夹具不应有任何警告
    assert outputs["json"].is_file() and outputs["markdown"].is_file()


def test_markdown_renders_all_sections(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h")
    scorecard, _ = reporting.run_scorecard(root, tmp_path / "out")
    markdown = (tmp_path / "out" / "scorecard.md").read_text(encoding="utf-8")
    for section in (
        "一、总体表现",
        "二、分 horizon 收益统计",
        "三、评分分层",
        "四、时间分层",
        "五、失败分析",
        "数据来源与口径",
        "注意事项",
        "ex-post",
    ):
        assert section in markdown
    assert "T+5" in markdown and "50.00%" in markdown  # 胜率渲染为百分比


# ---------------------------------------------------------------------------
# 2. horizon 边界：T+20/T+60 只读 outcome、只读保证、regime ex-post
# ---------------------------------------------------------------------------


def test_t20_t60_evaluation_only_from_outcome(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", horizons=(3, 5, 10, 15, 20, 60))
    scorecard, _ = reporting.run_scorecard(root, tmp_path / "out")
    assert scorecard["capability"]["outcome_horizons_present"] == [3, 5, 10, 15, 20, 60]
    assert scorecard["capability"]["horizons_unavailable"] == {}
    assert scorecard["horizon_stats"]["T+20"]["samples"] == 8
    t20_avg = scorecard["horizon_stats"]["T+20"]["average_return"]
    t60_avg = scorecard["horizon_stats"]["T+60"]["average_return"]
    assert t20_avg == pytest.approx(T5_MEAN * 3.0, abs=1e-9)
    assert t60_avg == pytest.approx(T5_MEAN * 6.0, abs=1e-9)
    assert "绝不修改训练 label" in scorecard["capability"]["t20_t60_usage"]


def test_run_is_read_only_on_inputs(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", horizons=(3, 5, 10, 15, 20, 60), extra_phase_b=True)
    before = _tree_hash(root)
    assert _run_cli(root, tmp_path / "out") == 0
    assert _tree_hash(root) == before  # 哈希 + mtime 全部不变
    assert {p.name for p in (tmp_path / "out").iterdir()} == {"scorecard.json", "scorecard.md"}


def test_market_regime_expost_only_when_present(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", frame_kwargs={"add_regime": True})
    scorecard, _ = reporting.run_scorecard(root, tmp_path / "out")
    regime = scorecard["market_regime"]
    assert regime["available"] is True
    assert "ex-post" in regime["note"] and "不得进入预测" in regime["note"]
    rows = {r["regime"]: r for r in regime["rows"]}
    assert rows["bull"]["samples"] == 4
    assert rows["bull"]["avg_return"] == pytest.approx(-0.0075, abs=1e-9)
    assert rows["bear"]["samples"] == 4
    assert rows["bear"]["avg_return"] == pytest.approx(0.02, abs=1e-9)


# ---------------------------------------------------------------------------
# 3. fail closed：缺数据 / 不一致 → 真实退出码，不产出半份报告
# ---------------------------------------------------------------------------


def test_missing_fold_files_fail_closed(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty" / "predictions"
    empty_root.mkdir(parents=True)
    assert _run_cli(tmp_path / "empty", tmp_path / "out") == 2
    assert not (tmp_path / "out").exists()


def test_missing_outcome_cache_fail_closed_and_degraded(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", include_cache=False)
    assert _run_cli(root, tmp_path / "out") == 3
    assert not (tmp_path / "out").exists()

    assert _run_cli(root, tmp_path / "out", "--allow-missing-outcomes") == 0
    scorecard = json.loads((tmp_path / "out" / "scorecard.json").read_text(encoding="utf-8"))
    assert list(scorecard["horizon_stats"]) == ["T+5"]
    assert set(scorecard["capability"]["horizons_unavailable"]) == {"3", "10", "15", "20", "60"}
    assert any("降级" in w for w in scorecard["warnings"])
    assert scorecard["horizon_stats"]["T+5"]["samples"] == 8  # F 的哨兵值同样剔除


def test_missing_run_manifest_fail_closed(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", include_manifest=False)
    assert _run_cli(root, tmp_path / "out") == 3
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("frame_kwargs", "extra_setup"),
    [
        ({"drop": frozenset({("2023-01-02", "D")})}, "预测行在 outcome 帧缺行"),
        ({"flip_executable": ("2023-01-02", "A")}, "executable 标志矛盾"),
        ({"tamper_5d": ("2023-01-02", "A")}, "5d 收益数值矛盾"),
    ],
)
def test_frame_inconsistency_fail_closed(
    tmp_path: Path, frame_kwargs: dict[str, Any], extra_setup: str
) -> None:
    root = _write_root(tmp_path / "m4h", frame_kwargs=frame_kwargs)
    assert _run_cli(root, tmp_path / "out") == 4, extra_setup
    assert not (tmp_path / "out").exists()


def test_schema_mismatch_fail_closed(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", bad_schema=True)
    assert _run_cli(root, tmp_path / "out") == 4
    assert not (tmp_path / "out").exists()


def test_protocol_mix_fail_closed(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", manifest_protocol="another_run")
    assert _run_cli(root, tmp_path / "out") == 4


def test_overlapping_folds_fail_closed(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", overlapping_fold=True)
    assert _run_cli(root, tmp_path / "out") == 4


def test_out_dir_inside_m4h_root_rejected(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h")
    before = _tree_hash(root)
    assert _run_cli(root, root / "p4_out") == 2
    assert not (root / "p4_out").exists()
    assert _tree_hash(root) == before  # root 未被写入


# ---------------------------------------------------------------------------
# 4. phase B 文件排除 + CLI 产物
# ---------------------------------------------------------------------------


def test_phase_b_files_excluded_from_pool(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", extra_phase_b=True)
    assert _run_cli(root, tmp_path / "out") == 0
    scorecard = json.loads((tmp_path / "out" / "scorecard.json").read_text(encoding="utf-8"))
    assert scorecard["overall"]["prediction_rows"] == 10  # 3 条 phase B 行不计入
    assert scorecard["overall"]["symbols_count"] == 7
    assert scorecard["schema"] == "alpha_v2_p4_scorecard.v1"
    assert scorecard["read_only"] is True
    assert scorecard["provenance"]["protocol_id"] == PROTOCOL_ID
    assert scorecard["provenance"]["benchmark_name_distribution"] == {"eligible_ew": 10}


# ---------------------------------------------------------------------------
# 5. manifest 指针失效时的 verified_fallback（真实 m4h 工件目录曾出现：
#    phase A manifest 引用的 cache 被清理，磁盘只剩内容一致的重命名 cache）
# ---------------------------------------------------------------------------


def test_stale_manifest_cache_resolved_by_verified_consistency(tmp_path: Path) -> None:
    root = _write_root(tmp_path / "m4h", cache_key="altkey", manifest_cache_key="gone")
    assert _run_cli(root, tmp_path / "out") == 0
    scorecard = json.loads((tmp_path / "out" / "scorecard.json").read_text(encoding="utf-8"))
    resolution = scorecard["provenance"]["outcome_cache_resolution"]
    assert resolution["mode"] == "verified_fallback"
    assert "gone" in resolution["manifest_ref"]
    assert "altkey" in resolution["path"]
    assert any("一致性验证" in w for w in scorecard["warnings"])
    assert scorecard["horizon_stats"]["T+5"]["samples"] == 8  # 统计不受解析方式影响
    markdown = (tmp_path / "out" / "scorecard.md").read_text(encoding="utf-8")
    assert "verified_fallback" in markdown


def test_stale_manifest_without_consistent_candidate_fail_closed(tmp_path: Path) -> None:
    # 唯一候选 cache 被篡改（5d 数值矛盾）→ 不接受任何"猜测"，维持 exit 3。
    root = _write_root(
        tmp_path / "m4h",
        cache_key="altkey",
        manifest_cache_key="gone",
        frame_kwargs={"tamper_5d": ("2023-01-02", "A")},
    )
    assert _run_cli(root, tmp_path / "out") == 3
    assert not (tmp_path / "out").exists()
