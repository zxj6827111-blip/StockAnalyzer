"""涨跌停补采器（``collect_stk_limit_history.py``）的失败口径测试。

钉住三件事：token 只按变量名读、正好 10,000 行按截断处理、失败日子必须进
``days_failed`` 并让进程非零退出 —— "部分覆盖"不得被读成"补齐了"。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
import tushare

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_stk_limit_history.py"


def _load():
    spec = importlib.util.spec_from_file_location("collect_stk_limit_history", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Pro:
    """替身：按日子返回固定行数，或对指定日子抛异常。"""

    def __init__(self, counts: dict[str, int], boom: tuple[str, ...] = ()):
        self.counts = counts
        self.boom = boom

    def stk_limit(self, *, trade_date: str, fields: str) -> pd.DataFrame:
        if trade_date in self.boom:
            raise OSError("Errno -3 Temporary failure in name resolution")
        n = self.counts.get(trade_date, 0)
        return pd.DataFrame({
            "trade_date": [trade_date] * n,
            "ts_code": [f"{i:06d}.SZ" for i in range(n)],
            "up_limit": [11.0] * n,
            "down_limit": [9.0] * n,
        })


def test_collect_writes_header_and_per_day_counts(tmp_path):
    module = _load()
    out = tmp_path / "limits.csv"
    summary = module.collect(
        _Pro({"20260611": 3, "20260612": 2}), ["20260611", "20260612"],
        str(out), retries=0, sleep=0.0, progress=False,
    )
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "trade_date,ts_code,up_limit,down_limit"
    assert len(lines) == 1 + 5
    assert summary["rows_total"] == 5
    assert summary["collected_days"] == 2
    assert summary["days_failed"] == {}
    assert summary["days_at_row_limit_possibly_truncated"] == []


def test_response_exactly_at_row_limit_is_flagged_not_trusted(tmp_path):
    module = _load()
    summary = module.collect(
        _Pro({"20260611": module.API_ROW_LIMIT}), ["20260611"], str(tmp_path / "l.csv"),
        retries=0, sleep=0.0, progress=False,
    )
    assert summary["days_at_row_limit_possibly_truncated"] == ["20260611"]


def test_failed_day_lands_in_days_failed_and_other_days_still_land(tmp_path):
    module = _load()
    pro = _Pro({"20260611": 4, "20260613": 6}, boom=("20260612",))
    out = tmp_path / "limits.csv"
    summary = module.collect(
        pro, ["20260611", "20260612", "20260613"], str(out),
        retries=0, sleep=0.0, progress=False,
    )
    assert list(summary["days_failed"]) == ["20260612"]
    assert summary["rows_total"] == 10
    # 失败的日子既不算"0 行"也不算"覆盖到了"
    assert summary["collected_days"] == 2


def test_null_limit_prices_are_counted_per_day(tmp_path):
    module = _load()
    frame = pd.DataFrame({
        "trade_date": ["20260611", "20260611"],
        "ts_code": ["000001.SZ", "000002.SZ"],
        "up_limit": [11.0, None],
        "down_limit": [9.0, None],
    })

    class _NullPro:
        def stk_limit(self, *, trade_date: str, fields: str):
            return frame

    summary = module.collect(
        _NullPro(), ["20260611"], str(tmp_path / "l.csv"),
        retries=0, sleep=0.0, progress=False,
    )
    assert summary["days_with_null_limit_prices"] == {"20260611": 2}


def test_missing_token_env_fails_closed(monkeypatch):
    module = _load()
    monkeypatch.delenv(module.DEFAULT_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit) as excinfo:
        module._resolve_token(module.DEFAULT_TOKEN_ENV)
    message = str(excinfo.value)
    assert module.DEFAULT_TOKEN_ENV in message
    assert "fail-closed" in message


def _run_main(module, tmp_path, monkeypatch, summary: dict):
    monkeypatch.setattr(module, "open_days", lambda *a, **k: ["20260611", "20260612"])
    monkeypatch.setattr(module, "collect", lambda *a, **k: summary)
    monkeypatch.setattr(tushare, "pro_api", lambda token: object())
    monkeypatch.setenv(module.DEFAULT_TOKEN_ENV, "token-value-not-printed")
    out = tmp_path / "limits.csv"
    report = tmp_path / "report.json"
    rc = module.main([
        "--start", "20260611", "--end", "20260612",
        "--out", str(out), "--report", str(report),
    ])
    return rc, report


def test_main_returns_error_when_any_day_failed(tmp_path, monkeypatch, capsys):
    module = _load()
    rc, report = _run_main(
        module, tmp_path, monkeypatch,
        {"days_failed": {"20260612": "RuntimeError: boom"}, "rows_total": 4},
    )
    assert rc == 4
    assert json.loads(report.read_text(encoding="utf-8"))["days_failed"]
    # token 取值不得出现在任何输出里
    assert "token-value-not-printed" not in capsys.readouterr().out


def test_main_returns_zero_when_every_day_landed(tmp_path, monkeypatch):
    module = _load()
    rc, _ = _run_main(
        module, tmp_path, monkeypatch,
        {"days_failed": {}, "rows_total": 8},
    )
    assert rc == 0


def test_days_alone_is_a_valid_invocation(tmp_path, monkeypatch):
    """只给 --days 也必须能跑：真实失败过一次的入口。"""
    module = _load()
    seen: dict = {}

    def _collect(pro, days, out_path, **kwargs):
        seen["days"] = days
        return {"days_failed": {}, "rows_total": 0}

    monkeypatch.setattr(module, "collect", _collect)
    monkeypatch.setattr(tushare, "pro_api", lambda token: object())
    monkeypatch.setenv(module.DEFAULT_TOKEN_ENV, "token-value-not-printed")
    rc = module.main([
        "--days", "20260611,20260612", "--out", str(tmp_path / "limits.csv"),
    ])
    assert rc == 0
    assert seen["days"] == ["20260611", "20260612"]


def test_no_days_and_no_range_is_rejected(tmp_path):
    module = _load()
    with pytest.raises(SystemExit) as excinfo:
        module.main(["--out", str(tmp_path / "limits.csv")])
    assert excinfo.value.code == 2
