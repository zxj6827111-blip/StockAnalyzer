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

import importlib.util
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
    """建最小可用的两表：signal_snapshots + outcome_records。"""
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
                [snapshot_id, decision_time, '{"score": 55.5}', "{}"],
            )
            con.execute(
                "INSERT INTO outcome_records VALUES (?, ?, ?)",
                [snapshot_id, realized, maturity],
            )
    finally:
        con.close()


def test_read_pairs_filters_immature_labels_but_reports_them(tmp_path: Path) -> None:
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
    pairs, tally = MEASURE._read_pairs(str(db), retries=1)
    assert [row[0] for row in pairs] == ["2026-09-10"] * 3
    # 分布按**未按成熟度过滤**口径统计：剔了多少必须看得见。注意 s5 收益为空，
    # 在 SQL 层就被 realized_return IS NOT NULL 挡掉，不进这个分布（故 pending 计 1）。
    assert tally == {"fully_matured": 1, "reconciled": 1, "label_matured": 1, "pending": 1}


def test_read_pairs_all_keeps_pending(tmp_path: Path) -> None:
    db = tmp_path / "learning_protocol.duckdb"
    _seed_db(
        db,
        [
            ("s1", "2026-09-10T14:30:00", 0.01, "reconciled"),
            ("s4", "2026-09-11T14:30:00", 0.99, "pending"),
        ],
    )
    pairs, _ = MEASURE._read_pairs(str(db), retries=1, maturity_statuses=())
    assert len(pairs) == 2


def test_cmdline_default_maturity_is_the_training_side_one(tmp_path: Path) -> None:
    """默认口径必须跟训练侧一致（label_matured/reconciled/fully_matured），不另立一套。"""
    assert set(MEASURE._DEFAULT_MATURITY_STATUSES) == {
        "label_matured",
        "reconciled",
        "fully_matured",
    }
    db = tmp_path / "learning_protocol.duckdb"
    _seed_db(db, [("s1", "2026-09-10T14:30:00", 0.01, "pending")])
    code = MEASURE.main(["--db", str(db), "--retries", "1", "--json"])
    assert code == 0


def test_selftest_passes() -> None:
    assert MEASURE.main(["--selftest"]) == 0
