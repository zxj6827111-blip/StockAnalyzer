"""生产票回测入口的取数口径测试。

钉的是两件最容易悄悄扭曲胜率的事：`entry_date` 取的是不是**下一个开市日**
（跨周末/长假不能按自然日推），以及同日多票多次扫描去重后**有没有把不同口径合并**。
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
from datetime import date
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "backtest_production_picks_tail_rules.py"
)
_spec = importlib.util.spec_from_file_location("bt_prod_picks", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
build_requests = _module.build_requests
group_labels = _module.group_labels

SESSIONS = [
    date(2026, 4, 30), date(2026, 5, 4), date(2026, 5, 5),  # 5/1-5/3 休市
]

HEADERS = [
    "snapshot_id", "symbol", "decision_date", "strategy", "capture_mode",
    "p_meta", "p_lgbm", "c_board", "c_completion", "c_news",
    "watchlist_source", "data_quality_score",
]


def _write(path: Path, rows: list[dict], *, zipped: bool = False) -> Path:
    target = Path(str(path) + ".gz") if zipped else path
    if zipped:
        handle = gzip.open(target, "wt", encoding="utf-8", newline="")
    else:
        handle = target.open("w", encoding="utf-8", newline="")
    with handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in HEADERS})
    return target


def _row(symbol: str, day: str, **extra) -> dict:
    base = {
        "snapshot_id": f"snap_{symbol}_{day}",
        "symbol": symbol,
        "decision_date": day,
        "strategy": "trend",
        "capture_mode": "observed_snapshot",
        "p_meta": "0.13",
    }
    base.update(extra)
    return base


def test_entry_date_skips_the_holiday_not_the_calendar_day(tmp_path: Path) -> None:
    csv_path = _write(tmp_path / "snap.csv", [_row("600000", "2026-04-30")])
    requests, stats = build_requests(
        csv_path, SESSIONS, strategies=("trend",), max_decision_date=date(2026, 5, 5),
    )
    assert [item["entry_date"] for item in requests] == ["2026-05-04"]
    assert stats["requests"] == 1


def test_last_session_has_no_next_session_so_it_is_dropped(tmp_path: Path) -> None:
    csv_path = _write(tmp_path / "snap.csv", [_row("600000", "2026-05-05")])
    requests, stats = build_requests(
        csv_path, SESSIONS, strategies=("trend",), max_decision_date=date(2026, 5, 5),
    )
    assert requests == []
    assert stats["skipped"]["no_next_session"] == 1


def test_cutoff_and_duplicate_rules_do_not_silently_merge_scopes(tmp_path: Path) -> None:
    rows = [
        _row("600000", "2026-04-30"),
        _row("600000", "2026-04-30"),  # 同日第二次扫描 → 去重
        _row("600001", "2026-04-30", capture_mode="replayed_recompute"),  # 口径不同 → 保留
        _row("600002", "2026-04-30", strategy="monster"),  # 另一个策略
        _row("600003", "2026-05-05"),  # 过窗口
    ]
    csv_path = _write(tmp_path / "snap.csv", rows)
    requests, stats = build_requests(
        csv_path, SESSIONS, strategies=("trend", "monster"),
        max_decision_date=date(2026, 5, 4),
    )
    keys = {(item["symbol"], item["capture_mode"], item["strategy"]) for item in requests}
    assert ("600000", "observed_snapshot", "trend") in keys
    assert ("600001", "replayed_recompute", "trend") in keys
    assert ("600002", "observed_snapshot", "monster") in keys
    assert all(item["decision_date"] <= "2026-05-04" for item in requests)
    assert stats["skipped"]["duplicate"] == 1
    assert stats["skipped"]["after_cutoff"] == 1


def test_gzipped_export_is_readable(tmp_path: Path) -> None:
    csv_path = _write(tmp_path / "snap.csv", [_row("600000", "2026-04-30")], zipped=True)
    requests, _ = build_requests(
        csv_path, SESSIONS, strategies=("trend",), max_decision_date=date(2026, 5, 5),
    )
    assert len(requests) == 1


def test_segments_keep_capture_modes_apart_in_the_rates(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    lines = [
        {"symbol": "600000", "decision_date": "2026-04-30", "strategy": "trend",
         "capture_mode": "observed_snapshot", "label": 1, "filled": True,
         "net_return": 0.05},
        {"symbol": "600001", "decision_date": "2026-04-30", "strategy": "trend",
         "capture_mode": "observed_snapshot", "label": 0, "filled": True,
         "net_return": -0.05},
        {"symbol": "600002", "decision_date": "2026-04-30", "strategy": "trend",
         "capture_mode": "replayed_recompute", "label": 1, "filled": True,
         "net_return": 0.08},
        # 未成交的行不进盈亏分子分母（§3.3）
        {"symbol": "600003", "decision_date": "2026-04-30", "strategy": "trend",
         "capture_mode": "observed_snapshot", "label": None, "filled": False},
    ]
    import json

    labels.write_text(
        "\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8",
    )
    segments = group_labels(labels, [])
    observed = segments["trend|observed_snapshot"]
    replayed = segments["trend|replayed_recompute"]
    assert observed["filled"] == 2 and observed["net_profit_rate"] == 0.5
    assert replayed["filled"] == 1 and replayed["net_profit_rate"] == 1.0
    assert observed["requests"] == 3  # 第四行是观察口径但没成交，仍计入 labelled 之外
