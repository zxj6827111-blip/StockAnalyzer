"""``scripts/sync_index_daily.py`` 双库增量契约（2026-10-04 指数断供修复）。

钉住四件事：

1. 双库都要喂（delta=生产运行时读、market.duckdb=回测面板读）——只喂一边
   就是当初"两库同停更 8/14"的翻版；
2. 增量窗口 = 两库最旧缺口并集（最新日期-5 日重叠），不回扫 --since 之前的
   历史（防意外差异产生巨量拉取）；
3. 双库都已是最新 → checkpoint_current 空转（不浪费 tushare 配额）；
4. tushare 空返回 / 无 token → skipped 且两库原样（fail-soft，不阻断 cron）。
"""

from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sync_index_daily", REPO_ROOT / "scripts" / "sync_index_daily.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path, *, last_date: str | None) -> None:
    """建一个只含 index_daily 语义的库（经 ensure_schema + 可选种子行）。"""
    module = _load_module()
    warehouse = module._warehouse(str(path))
    warehouse.ensure_schema()
    if last_date is not None:
        frame = pd.DataFrame(
            {
                "index_code": ["000300.SH"],
                "trade_date": [pd.Timestamp(last_date)],
                "close": [4000.0],
            }
        )
        warehouse.upsert_index_daily(frame=frame)


class _FakeProvider:
    """记录拉取窗口、返回固定行的最小 provider 替身。"""

    def __init__(self, *, rows_from: str, rows_to: str, empty: bool = False) -> None:
        self.rows_from = rows_from
        self.rows_to = rows_to
        self.empty = empty
        self.calls: list[tuple[str, str]] = []

    def fetch_index_daily(self, *, index_code: str, start_date, end_date):
        self.calls.append((str(start_date), str(end_date)))
        if self.empty:
            return pd.DataFrame()
        business = pd.bdate_range(self.rows_from, self.rows_to)
        return pd.DataFrame(
            {
                "index_code": [index_code] * len(business),
                "trade_date": business,
                "close": [4000.0 + i for i in range(len(business))],
            }
        )


def _max_date(module, db: Path):
    warehouse = module._warehouse(str(db))
    frame = warehouse.fetch_index_daily(index_code="000300.SH")
    dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
    return dates.max().date()


def test_sync_feeds_both_databases(tmp_path: Path) -> None:
    module = _load_module()
    delta_db = tmp_path / "market_delta.duckdb"
    market_db = tmp_path / "market.duckdb"
    _make_db(delta_db, last_date="2026-08-14")
    _make_db(market_db, last_date="2026-08-10")

    provider = _FakeProvider(rows_from="2026-08-01", rows_to="2026-08-20")
    result = module.sync_index_daily(
        date(2026, 8, 1),
        provider=provider,
        market_db=str(market_db),
        delta_db=str(delta_db),
    )
    assert result["status"] == "ok"
    # 两个库都被推进到 provider 行的最新日期。
    assert _max_date(module, delta_db) == date(2026, 8, 20)
    assert _max_date(module, market_db) == date(2026, 8, 20)


def test_sync_window_covers_oldest_gap_with_overlap(tmp_path: Path) -> None:
    module = _load_module()
    delta_db = tmp_path / "market_delta.duckdb"
    market_db = tmp_path / "market.duckdb"
    _make_db(delta_db, last_date="2026-08-14")
    _make_db(market_db, last_date="2026-08-05")

    provider = _FakeProvider(rows_from="2026-07-20", rows_to="2026-08-20")
    module.sync_index_daily(
        date(2026, 8, 1),
        provider=provider,
        market_db=str(market_db),
        delta_db=str(delta_db),
    )
    # 并集窗口取两库最旧（market 8/05）减 5 日重叠。
    expected_start = date(2026, 8, 5) - timedelta(days=5)
    assert provider.calls[0][0] == str(expected_start)


def test_sync_checkpoint_current_is_noop(tmp_path: Path) -> None:
    module = _load_module()
    delta_db = tmp_path / "market_delta.duckdb"
    market_db = tmp_path / "market.duckdb"
    # last-5 日重叠窗口的起点必须晚于今天：last = today+7 → start = today+2。
    future = (date.today() + timedelta(days=7)).isoformat()
    _make_db(delta_db, last_date=future)
    _make_db(market_db, last_date=future)

    provider = _FakeProvider(rows_from="2026-01-01", rows_to="2026-01-05")
    result = module.sync_index_daily(
        date(2026, 8, 1),
        provider=provider,
        market_db=str(market_db),
        delta_db=str(delta_db),
    )
    assert result == {"status": "skipped", "reason": "checkpoint_current"}
    assert provider.calls == []


def test_sync_empty_fetch_leaves_databases_untouched(tmp_path: Path) -> None:
    module = _load_module()
    delta_db = tmp_path / "market_delta.duckdb"
    market_db = tmp_path / "market.duckdb"
    _make_db(delta_db, last_date="2026-08-14")
    _make_db(market_db, last_date="2026-08-14")

    provider = _FakeProvider(rows_from="2026-08-01", rows_to="2026-08-20", empty=True)
    result = module.sync_index_daily(
        date(2026, 8, 1),
        provider=provider,
        market_db=str(market_db),
        delta_db=str(delta_db),
    )
    assert result == {"status": "skipped", "reason": "empty_fetch"}
    assert _max_date(module, delta_db) == date(2026, 8, 14)
    assert _max_date(module, market_db) == date(2026, 8, 14)


def test_sync_without_token_is_skipped(tmp_path: Path, monkeypatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_resolve_tushare_token", lambda: "")
    delta_db = tmp_path / "market_delta.duckdb"
    market_db = tmp_path / "market.duckdb"
    _make_db(delta_db, last_date="2026-08-14")
    _make_db(market_db, last_date="2026-08-14")

    # 不注入 provider：让函数走真实 token 解析分支。
    result = module.sync_index_daily(
        date(2026, 8, 1),
        market_db=str(market_db),
        delta_db=str(delta_db),
    )
    assert result == {"status": "skipped", "reason": "no_token"}
    assert _max_date(module, delta_db) == date(2026, 8, 14)
