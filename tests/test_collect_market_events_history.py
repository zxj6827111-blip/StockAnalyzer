"""第五类信息补采器（``collect_market_events_history.py``）的采集口径测试。

计划允许资金流/龙虎榜作为特征或风险信息进入，但前提是有可算输入；这里钉住
"采到的东西怎么算数"：列并集不静默丢数据、正好 10,000 行按截断处理、
失败日子必须让进程非零退出。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
import tushare

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_market_events_history.py"


def _load():
    spec = importlib.util.spec_from_file_location("collect_market_events_history", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Pro:
    def __init__(self, api: str, by_day: dict[str, pd.DataFrame], boom: tuple[str, ...] = ()):
        self.api = api
        self.by_day = by_day
        self.boom = boom

    def moneyflow(self, *, trade_date: str, fields: str) -> pd.DataFrame:
        return self._get(trade_date)

    def top_list(self, *, trade_date: str, fields: str) -> pd.DataFrame:
        return self._get(trade_date)

    def _get(self, trade_date: str) -> pd.DataFrame:
        if trade_date in self.boom:
            raise PermissionError("抱歉，您没有权限访问该接口")
        return self.by_day.get(trade_date, pd.DataFrame())


def _frame(day: str, n: int, **extra) -> pd.DataFrame:
    data = {
        "trade_date": [day] * n,
        "ts_code": [f"{i:06d}.SZ" for i in range(n)],
        "net_mf_amount": [1.5] * n,
    }
    data.update(extra)
    return pd.DataFrame(data)


def test_columns_are_the_union_across_days_not_the_first_day(tmp_path):
    """个别日子少回字段时，按首日取列会把后面的数据静默丢掉。"""
    module = _load()
    day_a = _frame("20250630", 2)
    day_b = _frame("20250930", 3, reason="涨跌幅偏离")
    out = tmp_path / "ev.csv"
    summary = module.collect(
        _Pro("moneyflow", {"20250630": day_a, "20250930": day_b}), "moneyflow",
        ["20250630", "20250930"], str(out), retries=0, sleep=0.0, progress=False,
    )
    assert "reason" in summary["columns"]
    rows = pd.read_csv(out)
    assert len(rows) == 5
    assert rows["reason"].isna().sum() == 2
    assert rows["reason"].notna().sum() == 3


def test_empty_day_is_separated_from_failed_day(tmp_path):
    module = _load()
    out = tmp_path / "ev.csv"
    summary = module.collect(
        _Pro("top_list", {"20250630": _frame("20250630", 4)}, boom=("20250701",)),
        "top_list", ["20250630", "20250701", "20250702"], str(out),
        retries=0, sleep=0.0, progress=False,
    )
    # 20250702 接口回了 0 行：那是"当天没有榜单"，不是失败
    assert summary["empty_days"] == ["20250702"]
    assert list(summary["days_failed"]) == ["20250701"]
    assert summary["rows_total"] == 4


def test_response_exactly_at_row_limit_is_flagged(tmp_path):
    module = _load()
    big = _frame("20250630", module.API_ROW_LIMIT)
    summary = module.collect(
        _Pro("moneyflow", {"20250630": big}), "moneyflow", ["20250630"],
        str(tmp_path / "ev.csv"), retries=0, sleep=0.0, progress=False,
    )
    assert summary["days_at_row_limit_possibly_truncated"] == ["20250630"]


def test_unknown_api_is_rejected_by_cli(tmp_path):
    module = _load()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--api", "limit_up", "--days", "20250630", "--out", str(tmp_path / "x.csv")])
    assert excinfo.value.code == 2


def test_no_days_and_no_range_is_rejected(tmp_path):
    module = _load()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--api", "moneyflow", "--out", str(tmp_path / "x.csv")])
    assert excinfo.value.code == 2


def test_missing_token_env_fails_closed(monkeypatch):
    module = _load()
    monkeypatch.delenv(module.DEFAULT_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        module._resolve_token(module.DEFAULT_TOKEN_ENV)
    assert module.DEFAULT_TOKEN_ENV in str(excinfo.value)


def test_main_exits_error_when_a_day_failed(tmp_path, monkeypatch, capsys):
    module = _load()
    pro = _Pro("moneyflow", {"20250630": _frame("20250630", 3)}, boom=("20250701",))
    monkeypatch.setattr(tushare, "pro_api", lambda token: pro)
    monkeypatch.setenv(module.DEFAULT_TOKEN_ENV, "token-value-not-printed")
    out = tmp_path / "ev.csv"
    report = tmp_path / "rep.json"
    rc = module.main([
        "--api", "moneyflow", "--days", "20250630,20250701",
        "--out", str(out), "--report", str(report), "--sleep", "0",
    ])
    assert rc == 4
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert list(payload["days_failed"]) == ["20250701"]
    assert "token-value-not-printed" not in capsys.readouterr().out


def test_limit_days_bounds_the_probe_run(tmp_path, monkeypatch):
    """权限试采用 --limit-days：先花 2 天确认接口能通，再决定要不要跑全年。"""
    module = _load()
    seen: dict[str, object] = {}
    pro = _Pro("moneyflow", {f"2025{i:04d}": _frame(f"2025{i:04d}", 1) for i in range(100, 200)})

    def _collect(real_pro, api, days, out_path, **kwargs):
        seen["days"] = days
        return {"days_failed": {}, "rows_total": 0, "requested_days": len(days)}

    monkeypatch.setattr(module, "collect", _collect)
    monkeypatch.setattr(tushare, "pro_api", lambda token: pro)
    monkeypatch.setattr(
        module, "open_days",
        lambda *a, **k: sorted(f"2025{i:04d}" for i in range(100, 200)),
    )
    monkeypatch.setenv(module.DEFAULT_TOKEN_ENV, "token-value-not-printed")
    rc = module.main([
        "--api", "moneyflow", "--start", "20250101", "--end", "20251231",
        "--limit-days", "2", "--out", str(tmp_path / "ev.csv"),
    ])
    assert rc == 0
    assert seen["days"] == ["20250100", "20250101"]
