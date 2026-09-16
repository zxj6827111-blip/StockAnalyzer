"""`scripts/measure_score_return.py` 的判据契约。

这个脚本要回答的是一道**会改变生产决策**的题："夜扫长期 0 个 final signal，
该改阈值还是该认为分数没有效力？"两种解释的处置完全相反（改按分位选股 vs 不许放宽），
所以判据本身必须先被钉住：

1. **成对性**：分位收益只能用同一个样本的（分数, 收益）算——早先两侧各自过滤 NaN，
   "有限分数 + NaN 收益"与"NaN 分数 + 有限收益"会被错位配成一条，污染最高分档；
2. **标签成熟口径**：pending 的 realized_return 是中途市值标记，不是模型学过的那个
   标签，且过滤必须留下"剔掉了什么"的证据；
3. **三态判读**：正相关 → 阈值问题；负相关 → 效力问题；CI 跨 0 → 证据不足。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import random
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "measure_score_return", REPO_ROOT / "scripts" / "measure_score_return.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEASURE = _load_module()


# --- 判据三态 -----------------------------------------------------------------


def _positive_pairs(days: int = 20, symbols: int = 8) -> list[tuple[str, float, float]]:
    """分数越高收益越高（日内横截面强正相关）。"""
    return [
        (f"2026-01-{day:02d}", float(rank), 0.001 * rank)
        for day in range(1, days + 1)
        for rank in range(symbols)
    ]


def test_positive_relation_reads_as_threshold_issue() -> None:
    report = MEASURE.evaluate_pairs(_positive_pairs())
    assert report["verdict"].startswith("THRESHOLD_ISSUE")
    assert report["ic_mean"] > 0
    assert report["ic_ci95"][0] > 0
    assert report["quantiles"]["top_minus_bottom"] > 0


def test_inverted_relation_reads_as_efficacy_issue() -> None:
    """分数与收益显著负相关：放宽阈值只会把坏票放进来，必须判"阈值无解"。"""
    inverted = [
        (f"2026-01-{day:02d}", float(rank), -0.001 * rank)
        for day in range(1, 21)
        for rank in range(8)
    ]
    report = MEASURE.evaluate_pairs(inverted)
    assert report["verdict"].startswith("EFFICACY_ISSUE")
    assert report["ic_ci95"][1] < 0


def test_pure_noise_reads_as_inconclusive() -> None:
    """日内打乱对应关系（固定种子，断言确定性，不引入 flaky）。"""
    rng = random.Random(20260916)
    noise: list[tuple[str, float, float]] = []
    for day in range(1, 21):
        values = [0.001 * rank for rank in range(8)]
        rng.shuffle(values)
        for rank in range(8):
            noise.append((f"2026-01-{day:02d}", float(rank), values[rank]))
    report = MEASURE.evaluate_pairs(noise)
    assert report["verdict"].startswith("INCONCLUSIVE")


# --- 成对性：不可能点不得污染分位 ---------------------------------------------


def test_unpaired_points_do_not_move_quantiles() -> None:
    """对抗测试：塞进"有限分数+NaN 收益"和"NaN 分数+有限收益"两条，分位结果必须不变。

    这正是修掉的那个错位 bug——两侧各自过滤时，最高分那条会拿到别人的收益，
    而 top−bottom 恰恰是本脚本用来判"该不该改按分位选股"的那个数。
    """
    clean = _positive_pairs()
    base = MEASURE.evaluate_pairs(clean)
    padded = MEASURE.evaluate_pairs(
        [
            *clean,
            ("2026-01-21", float("nan"), -0.9),  # 有收益没分数
            ("2026-01-21", 9.0, float("nan")),  # 有分数没收益（且是最高分）
        ]
    )
    assert padded["quantiles"] == base["quantiles"]
    assert padded["threshold_stats"] == base["threshold_stats"]
    assert padded["top_k_stats"] == base["top_k_stats"]
    assert padded["samples"] == base["samples"]


def test_highest_score_ignores_label_availability() -> None:
    """ "离门槛还差几分"问的是系统产出的最高分，与这条有没有结算无关。"""
    pairs = [*_positive_pairs(days=5), ("2026-01-06", 88.0, float("nan"))]
    report = MEASURE.evaluate_pairs(pairs)
    assert report["highest_score"] == pytest.approx(88.0)
    assert report["nearest_to_threshold"]["distance_to_70"] == pytest.approx(70.0 - 88.0, abs=1e-4)


# --- 标签成熟口径与读库路径 ----------------------------------------------------


def _seed_db(path: Path, rows: list[tuple[str, str, float | None, str]]) -> None:
    """建最小可用的两表：signal_snapshots + outcome_records。

    注意 signal_snapshots 只存打分的**输入分量**（lgbm/xgb/meta/...），**没有 0~100 总分**——
    这正是 2026-09-16 修正数据源的原因：总分只出现在夜扫产物里。
    """
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE signal_snapshots (snapshot_id VARCHAR PRIMARY KEY, "
            "decision_time VARCHAR, score_breakdown_json VARCHAR, model_outputs_json VARCHAR)"
        )
        con.execute(
            "CREATE TABLE outcome_records (snapshot_id VARCHAR PRIMARY KEY, "
            "realized_return DOUBLE, maturity_status VARCHAR)"
        )
        for snapshot_id, decision_time, realized, maturity in rows:
            con.execute(
                "INSERT INTO signal_snapshots VALUES (?, ?, ?, ?)",
                [snapshot_id, decision_time, '{"lgbm": 0.5, "xgb": 0.5, "meta": 0.5}', "{}"],
            )
            con.execute(
                "INSERT INTO outcome_records VALUES (?, ?, ?)",
                [snapshot_id, realized, maturity],
            )
    finally:
        con.close()


def _write_artifact(
    root: Path,
    run_id: str,
    *,
    timestamp: str,
    candidates: list[dict[str, object]],
    commit: str = "deadbeefcafe",
    group: str = "heavy",
) -> Path:
    """写一份最小夜扫产物（结构照抄生产：results[0].payload.report.source_report...）。"""
    path = root / group / f"week5_night_scan.{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job": "week5_night_scan",
        "run_id": run_id,
        "timestamp": timestamp,
        "status": "success",
        "build": {"commit": commit},
        "results": [
            {
                "job": "week5_night_scan",
                "ran": True,
                "success": True,
                "detail": "week5_automation:ok",
                "payload": {
                    "report": {
                        "night_pool": candidates[:1],
                        "source_report": {
                            "signal_pool": {
                                "candidate_count": len(candidates),
                                "candidates": candidates,
                            }
                        },
                    }
                },
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_read_candidates_takes_score_from_artifact_cross_section(tmp_path: Path) -> None:
    """分数取自产物横截面（signal_pool.candidates），不是只有 0~1 条的 night_pool。"""
    root = tmp_path / "results"
    _write_artifact(
        root,
        "run1",
        timestamp="2026-09-15T21:45:04",
        candidates=[
            {"snapshot_id": f"s{i}", "symbol": f"{i:06d}", "score": 70.0 - i, "grade": "A"}
            for i in range(5)
        ],
    )
    candidates, summary = MEASURE._read_candidates(str(root))
    assert len(candidates) == 5
    assert summary["runs_with_candidates"] == 1
    assert summary["night_pool_total"] == 1  # 最终池单独计数，不进横截面
    assert summary["commit_set"] == ["deadbeefcafe"]


def test_read_candidates_dedups_by_snapshot_and_counts_bad_runs(tmp_path: Path) -> None:
    """同一 snapshot 多次出现取最后一次；产物损坏只计数，不让整次测量失效。"""
    root = tmp_path / "results"
    _write_artifact(
        root,
        "run1",
        timestamp="2026-09-15T21:45:04",
        candidates=[{"snapshot_id": "s1", "symbol": "000001", "score": 60.0}],
    )
    _write_artifact(
        root,
        "run2",
        timestamp="2026-09-15T22:45:04",
        candidates=[{"snapshot_id": "s1", "symbol": "000001", "score": 66.0}],
    )
    (root / "heavy" / "week5_night_scan.broken.json").write_text("{not json", encoding="utf-8")
    candidates, summary = MEASURE._read_candidates(str(root))
    assert len(candidates) == 1
    assert candidates[0]["score"] == 66.0
    assert summary["runs_unreadable"] == 1


def test_read_returns_filters_immature_labels_but_reports_them(tmp_path: Path) -> None:
    db = tmp_path / "learning_protocol.duckdb"
    _seed_db(
        db,
        [
            ("s1", "2026-09-10T14:30:00", 0.01, "fully_matured"),
            ("s2", "2026-09-10T14:30:00", 0.02, "reconciled"),
            ("s3", "2026-09-10T14:30:00", 0.03, "label_matured"),
            ("s4", "2026-09-11T14:30:00", 0.99, "pending"),  # 中途市值标记，必须剔除
            ("s5", "2026-09-11T14:30:00", None, "pending"),  # 未结算
        ],
    )
    got, ledger = MEASURE._read_returns(
        str(db), ["s1", "s2", "s3", "s4", "s5", "missing"], retries=1
    )
    assert sorted(got) == ["s1", "s2", "s3"]
    # 决策日取库里的 decision_time，不靠产物时间戳猜
    assert got["s1"][0] == "2026-09-10"
    assert ledger["not_in_outcome_records"] == 1
    assert ledger["return_null"] == 1
    assert ledger["immature_or_other"] == 1
    assert ledger["maturity_breakdown"] == {
        "fully_matured": 1,
        "reconciled": 1,
        "label_matured": 1,
        "pending": 2,
    }


def test_read_returns_all_keeps_pending(tmp_path: Path) -> None:
    db = tmp_path / "learning_protocol.duckdb"
    _seed_db(
        db,
        [
            ("s1", "2026-09-10T14:30:00", 0.01, "reconciled"),
            ("s4", "2026-09-11T14:30:00", 0.99, "pending"),
        ],
    )
    got, _ = MEASURE._read_returns(str(db), ["s1", "s4"], retries=1, maturity_statuses=())
    assert len(got) == 2


def test_thin_sample_refuses_a_verdict(tmp_path: Path) -> None:
    """样本不够时必须说"证据不足"，不能把噪声说成定论。

    这是本脚本的底线：它存在的意义就是阻止"为了让输出非空而放宽门禁"。
    """
    root = tmp_path / "results"
    db = tmp_path / "learning_protocol.duckdb"
    _seed_db(db, [])
    con = duckdb.connect(str(db))
    try:
        for i in range(3):
            con.execute(
                "INSERT INTO signal_snapshots VALUES (?, ?, ?, ?)",
                [f"s{i}", "2026-09-10T14:30:00", "{}", "{}"],
            )
            con.execute(
                "INSERT INTO outcome_records VALUES (?, ?, ?)", [f"s{i}", 0.01, "reconciled"]
            )
    finally:
        con.close()
    _write_artifact(
        root,
        "run1",
        timestamp="2026-09-15T21:45:04",
        candidates=[
            {"snapshot_id": f"s{i}", "symbol": f"{i:06d}", "score": 60.0 + i} for i in range(3)
        ],
    )
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        code = MEASURE.main(
            ["--db", str(db), "--artifacts-root", str(root), "--retries", "1", "--json"]
        )
    assert code == 0
    report = json.loads(buf.getvalue())
    assert report["verdict"].startswith("INSUFFICIENT_SAMPLE")
    assert report["samples"] == 3


def test_cmdline_default_maturity_is_the_training_side_one(tmp_path: Path) -> None:
    """默认口径必须跟训练侧一致（label_matured/reconciled/fully_matured），不另立一套。"""
    assert set(MEASURE._DEFAULT_MATURITY_STATUSES) == {
        "label_matured",
        "reconciled",
        "fully_matured",
    }
    db = tmp_path / "learning_protocol.duckdb"
    _seed_db(db, [("s1", "2026-09-10T14:30:00", 0.01, "pending")])
    root = tmp_path / "results"
    _write_artifact(
        root,
        "run1",
        timestamp="2026-09-15T21:45:04",
        candidates=[{"snapshot_id": "s1", "symbol": "000001", "score": 60.0}],
    )
    code = MEASURE.main(
        ["--db", str(db), "--artifacts-root", str(root), "--retries", "1", "--json"]
    )
    assert code == 0


def test_selftest_passes() -> None:
    assert MEASURE.main(["--selftest"]) == 0
