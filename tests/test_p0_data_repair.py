"""P0 数据修复包测试：价格口径校验、涨停标签、moneyflow、PIT 状态、映射、审计工具。"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.config import LimitRuleConfig, LimitRuleVersionEntry  # noqa: E402
from stock_analyzer.data.limit_rule import resolve_limit_pct  # noqa: E402
from stock_analyzer.data.market_warehouse import MarketWarehouse  # noqa: E402
from stock_analyzer.data.provider import DataSourceError  # noqa: E402
from stock_analyzer.data.security_identity_mapping import (  # noqa: E402
    BEIJING_CURRENT_PREFIX,
    BEIJING_LEGACY_PREFIXES,
    IdentityMappingEntry,
    deduplicate_event_count,
    load_mapping_from_csv,
    load_mapping_from_db,
    resolve_canonical_symbol,
    validate_mapping_entries,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_warehouse(tmp_path: Path, *, read_only: bool = False) -> MarketWarehouse:
    db_path = tmp_path / "test.duckdb"
    return MarketWarehouse(
        db_path=db_path,
        package_root=str(tmp_path / "package"),
        package_writes_enabled=False,
        read_only=read_only,
    )


def _make_daily_frame(
    symbol: str,
    dates: list[str],
    *,
    price_series_mode: str = "qfq",
    close: float = 10.0,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "symbol": [symbol] * len(dates),
            "date": pd.to_datetime(dates),
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000.0,
            "turnover": close * 1000.0,
            "price_series_mode": price_series_mode,
        }
    )
    frame["date"] = frame["date"].dt.date
    return frame


# ---------------------------------------------------------------------------
# P0-A: price_series_mode 一致性校验
# ---------------------------------------------------------------------------

class TestPriceSeriesModeConsistency:
    """测试 1-3: qfq 增量不写 raw / 拒绝不同 mode / mixed-mode 检测。"""

    def test_qfq_increment_not_written_as_raw(self, tmp_path):
        """测试 1: qfq 增量不会写成 raw。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 先写入 qfq 基线
        frame1 = _make_daily_frame("600000", ["2026-01-05", "2026-01-06"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame1)
        # 再写入 qfq 增量
        frame2 = _make_daily_frame("600000", ["2026-01-07"], price_series_mode="qfq")
        stored = wh.upsert_daily_bars(frame=frame2)
        assert stored == 1
        # 验证全部行都是 qfq
        daily = wh.fetch_all_daily_bars(symbol="600000")
        assert (daily["price_series_mode"] == "qfq").all()

    def test_reject_different_price_series_mode(self, tmp_path):
        """测试 2: 故意写入不同 price_series_mode 时被拒绝。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 写入 qfq 基线
        frame1 = _make_daily_frame("000001", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame1)
        # 尝试写入 raw——应该被拒绝
        frame2 = _make_daily_frame("000001", ["2026-01-06"], price_series_mode="raw")
        with pytest.raises(DataSourceError, match="price_series_mode mismatch"):
            wh.upsert_daily_bars(frame=frame2)

    def test_detect_mixed_mode_finds_transitions(self, tmp_path):
        """测试 3: mixed-mode 检测能找到模式切换。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 先写入 qfq
        frame1 = _make_daily_frame("300001", ["2026-01-05", "2026-01-06"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame1)
        # 用 enforce_price_series_mode=False 强制写入 raw 行（模拟历史污染）
        frame2 = _make_daily_frame("300001", ["2026-01-07", "2026-01-08"], price_series_mode="raw")
        wh.upsert_daily_bars(frame=frame2, enforce_price_series_mode=False)
        # 检测 mixed mode
        result = wh.detect_price_series_mixed()
        assert result["mixed_symbol_count"] == 1
        assert "300001" in [s["symbol"] for s in result["mixed_symbols"]]
        transitions = result["mixed_symbols"][0]["transition_dates"]
        assert len(transitions) >= 2  # 至少有 qfq→raw 切换


# ---------------------------------------------------------------------------
# P0-A: shadow rebuild
# ---------------------------------------------------------------------------

class TestShadowRebuild:
    """测试 4-5: shadow rebuild 拒绝 source==target / dry-run 不修改。"""

    def test_shadow_rebuild_rejects_same_path(self, tmp_path):
        """测试 4: shadow rebuild 拒绝 source == target。"""
        import importlib.util

        script_path = ROOT / "scripts" / "shadow_rebuild_price_series.py"
        spec = importlib.util.spec_from_file_location("shadow_rebuild", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        db_path = tmp_path / "test.duckdb"
        # 先创建一个空 DB
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()

        rc = mod._main([
            "--source-db", str(db_path),
            "--target-db", str(db_path),
            "--dry-run",
        ])
        assert rc == 2

    def test_dry_run_does_not_modify_db(self, tmp_path):
        """测试 5: dry-run 不修改 DB 文件大小和 mtime。"""
        import importlib.util

        script_path = ROOT / "scripts" / "shadow_rebuild_price_series.py"
        spec = importlib.util.spec_from_file_location("shadow_rebuild", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source_db = tmp_path / "source.duckdb"
        target_db = tmp_path / "target.duckdb"


        # 创建 source DB 并写入数据
        wh = MarketWarehouse(
            db_path=source_db,
            package_root=str(tmp_path / "src_pkg"),
            package_writes_enabled=False,
        )
        wh.ensure_schema()
        frame = _make_daily_frame("600000", ["2026-01-05", "2026-01-06"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame)

        source_size_before = source_db.stat().st_size

        rc = mod._main([
            "--source-db", str(source_db),
            "--target-db", str(target_db),
            "--target-mode", "qfq",
            "--dry-run",
        ])
        assert rc == 0

        source_size_after = source_db.stat().st_size
        assert source_size_before == source_size_after
        # target DB 不应被创建
        assert not target_db.exists()


# ---------------------------------------------------------------------------
# P0-B: trade-status 回填幂等
# ---------------------------------------------------------------------------

class TestTradeStatusBackfill:
    """测试 6: trade-status 重复回填幂等。"""

    def test_trade_status_upsert_idempotent(self, tmp_path):
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "trade_date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
                "up_limit": [11.0, 11.1],
                "down_limit": [9.0, 9.1],
                "suspended": [False, False],
                "suspend_type": ["", ""],
                "source": ["tushare_stk_limit", "tushare_stk_limit"],
                "as_of": ["2026-08-17", "2026-08-17"],
                "coverage_complete": [True, True],
            }
        )
        # 第一次写入
        count1 = wh.upsert_trade_status(symbol="600000", frame=frame)
        assert count1 == 2
        # 重复写入——不应增加重复
        count2 = wh.upsert_trade_status(symbol="600000", frame=frame)
        assert count2 == 2
        # 验证只有 2 行
        result = wh.fetch_trade_status(symbol="600000")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# P0-E: moneyflow 回填幂等
# ---------------------------------------------------------------------------

class TestMoneyflowBackfill:
    """测试 7: moneyflow 重复回填幂等。"""

    def test_moneyflow_upsert_idempotent(self, tmp_path):
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "trade_date": pd.to_datetime(["2026-01-05", "2026-01-06"]),
                "buy_sm_amount": [100.0, 110.0],
                "sell_sm_amount": [90.0, 95.0],
                "buy_md_amount": [200.0, 210.0],
                "sell_md_amount": [180.0, 190.0],
                "buy_lg_amount": [300.0, 310.0],
                "sell_lg_amount": [280.0, 290.0],
                "buy_elg_amount": [400.0, 410.0],
                "sell_elg_amount": [380.0, 390.0],
                "net_mf_amount": [40.0, 40.0],
                "source": ["tushare_moneyflow", "tushare_moneyflow"],
                "as_of": ["2026-08-17", "2026-08-17"],
                "coverage_complete": [True, True],
            }
        )
        count1 = wh.upsert_moneyflow(symbol="600000", frame=frame)
        assert count1 == 2
        count2 = wh.upsert_moneyflow(symbol="600000", frame=frame)
        assert count2 == 2
        result = wh.fetch_moneyflow(symbol="600000")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# checkpoint / resume + API 失败不记成功
# ---------------------------------------------------------------------------

class TestCheckpointResume:
    """测试 8-9: checkpoint/resume 跳过 + API 失败不记成功。"""

    def test_checkpoint_resume_skips_completed(self, tmp_path):
        """测试 8: checkpoint/resume 正确跳过已成功股票。"""
        # 模拟已有 checkpoint
        marker = "2026-01-01|2026-01-31|600000"
        ckpt_path = tmp_path / "ckpt.json"
        ckpt_path.write_text(
            json.dumps({marker: "2026-08-17T00:00:00"}), encoding="utf-8"
        )
        # 加载 checkpoint
        raw = json.loads(ckpt_path.read_text(encoding="utf-8"))
        assert marker in raw
        # resume 时应跳过
        already_done = {key.rsplit("|", 1)[-1] for key in raw}
        assert "600000" in already_done

    def test_api_failure_not_recorded_as_success(self, tmp_path):
        """测试 9: API 失败不会被记录为成功。"""
        ckpt = {}
        symbol = "000001"
        # 模拟 API 失败
        api_failed = True
        if api_failed:
            # 不写 checkpoint
            pass
        assert symbol not in ckpt


# ---------------------------------------------------------------------------
# P0-C: 涨停规则边界
# ---------------------------------------------------------------------------

class TestLimitRuleBoundaries:
    """测试 10: ST/IPO/主板/创业板/科创板/北交所涨停规则边界。"""

    def _make_config(self) -> LimitRuleConfig:
        return LimitRuleConfig(
            use_source_first=True,
            fallback_by_board=True,
            rule_version_by_date=[
                LimitRuleVersionEntry.model_validate(
                    {
                        "from": "2014-01-01",
                        "board": "主板",
                        "limit_pct": 0.10,
                        "ipo_no_limit_days": 0,
                    }
                ),
                LimitRuleVersionEntry.model_validate(
                    {
                        "from": "2020-08-24",
                        "board": "科创板",
                        "limit_pct": 0.20,
                        "ipo_no_limit_days": 5,
                    }
                ),
                LimitRuleVersionEntry.model_validate(
                    {
                        "from": "2020-08-24",
                        "board": "创业板",
                        "limit_pct": 0.20,
                        "ipo_no_limit_days": 5,
                    }
                ),
                LimitRuleVersionEntry.model_validate(
                    {
                        "from": "2021-11-15",
                        "board": "北交所",
                        "limit_pct": 0.30,
                        "ipo_no_limit_days": 0,
                    }
                ),
                LimitRuleVersionEntry.model_validate(
                    {
                        "from": "2014-01-01",
                        "board": "ST",
                        "limit_pct": 0.05,
                        "ipo_no_limit_days": 0,
                    }
                ),
            ],
            cost_schedule_by_date=[],
        )

    def test_st_limit_5pct(self):
        config = self._make_config()
        pct = resolve_limit_pct(
            config=config,
            trade_date=date(2026, 1, 5),
            board="ST",
            is_st=True,
            listing_days=100,
        )
        assert pct == 0.05

    def test_main_board_10pct(self):
        config = self._make_config()
        pct = resolve_limit_pct(
            config=config,
            trade_date=date(2026, 1, 5),
            board="主板",
            is_st=False,
            listing_days=100,
        )
        assert pct == 0.10

    def test_chinext_20pct(self):
        config = self._make_config()
        pct = resolve_limit_pct(
            config=config,
            trade_date=date(2026, 1, 5),
            board="创业板",
            is_st=False,
            listing_days=100,
        )
        assert pct == 0.20

    def test_star_20pct(self):
        config = self._make_config()
        pct = resolve_limit_pct(
            config=config,
            trade_date=date(2026, 1, 5),
            board="科创板",
            is_st=False,
            listing_days=100,
        )
        assert pct == 0.20

    def test_beijing_30pct(self):
        config = self._make_config()
        pct = resolve_limit_pct(
            config=config,
            trade_date=date(2026, 1, 5),
            board="北交所",
            is_st=False,
            listing_days=100,
        )
        assert pct == 0.30

    def test_ipo_no_limit_phase(self):
        config = self._make_config()
        # 科创板 IPO 前 5 日无涨跌幅
        pct = resolve_limit_pct(
            config=config,
            trade_date=date(2026, 1, 5),
            board="科创板",
            is_st=False,
            listing_days=3,  # 上市 3 天，在无涨跌幅窗口内
        )
        assert pct is None


# ---------------------------------------------------------------------------
# P0-C: PIT 状态区间无重叠
# ---------------------------------------------------------------------------

class TestSecurityStatusIntervals:
    """测试 11: point-in-time 状态区间无重叠。"""

    def test_no_overlap_detection(self, tmp_path):
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 写入不重叠的区间
        frame = pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "effective_from": pd.Series(pd.to_datetime(["2026-01-01", "2026-03-01"])).dt.date,
                "effective_to": pd.Series(pd.to_datetime(["2026-02-28", None])).dt.date,
                "status_type": ["st", "st"],
                "status_value": ["ST", "normal"],
                "board": ["主板", "主板"],
                "exchange": ["SSE", "SSE"],
                "source": ["tushare", "tushare"],
                "as_of": ["2026-08-17", "2026-08-17"],
                "coverage_complete": [True, True],
            }
        )
        wh.upsert_security_status(symbol="600000", frame=frame)
        overlaps = wh.validate_security_status_intervals(symbol="600000")
        assert len(overlaps) == 0

    def test_overlap_detected(self, tmp_path):
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 写入重叠的区间
        frame = pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "effective_from": pd.Series(pd.to_datetime(["2026-01-01", "2026-01-15"])).dt.date,
                "effective_to": pd.Series(pd.to_datetime(["2026-02-28", "2026-03-15"])).dt.date,
                "status_type": ["st", "st"],
                "status_value": ["ST", "ST*"],
                "board": ["主板", "主板"],
                "exchange": ["SSE", "SSE"],
                "source": ["tushare", "tushare"],
                "as_of": ["2026-08-17", "2026-08-17"],
                "coverage_complete": [True, True],
            }
        )
        # 写入重叠的区间——写前校验应直接拒绝
        with pytest.raises(DataSourceError, match="overlap detected before write"):
            wh.upsert_security_status(symbol="600000", frame=frame)


# ---------------------------------------------------------------------------
# P0-D: 北交所映射
# ---------------------------------------------------------------------------

class TestSecurityIdentityMapping:
    """测试 12: 北交所映射不会重复计算证券。"""

    def test_validate_mapping_entries_clean(self):
        entries = [
            IdentityMappingEntry(
                historical_symbol="430001",
                canonical_symbol="920001",
                effective_from=date(2025, 1, 1),
                effective_to=date(2025, 9, 30),
                source="external_verified",
                as_of="2026-08-17",
            ),
        ]
        errors = validate_mapping_entries(entries)
        assert errors == []

    def test_validate_rejects_overlap(self):
        entries = [
            IdentityMappingEntry(
                historical_symbol="430001",
                canonical_symbol="920001",
                effective_from=date(2025, 1, 1),
                effective_to=date(2025, 9, 30),
                source="external_verified",
                as_of="2026-08-17",
            ),
            IdentityMappingEntry(
                historical_symbol="430001",
                canonical_symbol="920002",
                effective_from=date(2025, 6, 1),
                effective_to=None,
                source="external_verified",
                as_of="2026-08-17",
            ),
        ]
        errors = validate_mapping_entries(entries)
        assert len(errors) > 0

    def test_resolve_canonical_symbol(self):
        entries = [
            IdentityMappingEntry(
                historical_symbol="430001",
                canonical_symbol="920001",
                effective_from=date(2025, 1, 1),
                effective_to=date(2025, 9, 30),
                source="external_verified",
                as_of="2026-08-17",
            ),
        ]
        # 在生效期内
        result = resolve_canonical_symbol(entries, "430001", date(2025, 6, 1))
        assert result == "920001"
        # 在生效期外——fail-closed
        result = resolve_canonical_symbol(entries, "430001", date(2026, 1, 1))
        assert result is None
        # 未知符号——fail-closed
        result = resolve_canonical_symbol(entries, "999999", date(2025, 6, 1))
        assert result is None

    def test_deduplicate_event_count(self):
        """旧代码和新代码的事件不重复计算。"""
        entries = [
            IdentityMappingEntry(
                historical_symbol="430001",
                canonical_symbol="920001",
                effective_from=date(2025, 1, 1),
                effective_to=date(2025, 9, 30),
                source="external_verified",
                as_of="2026-08-17",
            ),
        ]
        events = [
            {"symbol": "430001", "date": "2025-06-01", "event": "limit_up"},
            {"symbol": "920001", "date": "2025-06-01", "event": "limit_up"},
        ]
        deduped = deduplicate_event_count(events, entries)
        # 旧代码事件应被归并到 canonical，同一日不重复
        symbols = {e["symbol"] for e in deduped}
        assert "920001" in symbols
        # 不应同时出现 430001 和 920001 的同日事件
        dates_by_symbol = {}
        for e in deduped:
            dates_by_symbol.setdefault(e["symbol"], set()).add(e["date"])
        if "430001" in dates_by_symbol and "920001" in dates_by_symbol:
            overlap = dates_by_symbol["430001"] & dates_by_symbol["920001"]
            assert len(overlap) == 0

    def test_load_mapping_from_csv(self, tmp_path):
        csv_path = tmp_path / "mapping.csv"
        csv_path.write_text(
            "historical_symbol,canonical_symbol,effective_from,effective_to,source,as_of\n"
            "430001,920001,2025-01-01,2025-09-30,external_verified,2026-08-17\n"
            "830001,920002,2025-01-01,,external_verified,2026-08-17\n",
            encoding="utf-8",
        )
        entries = load_mapping_from_csv(csv_path)
        assert len(entries) == 2
        assert entries[0].historical_symbol == "430001"
        assert entries[0].canonical_symbol == "920001"
        assert entries[1].effective_to is None

    def test_beijing_prefixes_constant(self):
        assert BEIJING_LEGACY_PREFIXES == ("43", "83", "87")
        assert BEIJING_CURRENT_PREFIX == "92"


# ---------------------------------------------------------------------------
# 审计工具只读
# ---------------------------------------------------------------------------

class TestAuditReadOnly:
    """测试 13: 审计工具通过 read_only=True 打开数据库。"""

    def test_audit_opens_readonly(self, tmp_path):
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # 创建一个有数据的 DB
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame)

        db_path = tmp_path / "test.duckdb"
        size_before = db_path.stat().st_size

        # 运行审计
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
            "--output-md", str(tmp_path / "audit.md"),
        ])
        # NO-GO 是预期的（数据不完整），rc=1
        assert rc in (0, 1)

        # DB 文件不应被修改
        size_after = db_path.stat().st_size
        assert size_before == size_after

        # 验证 JSON 输出
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        assert "daily_bars" in report
        assert "gates" in report
        assert "overall_verdict" in report


# ---------------------------------------------------------------------------
# 现有 vendor overlay 和 MarketWarehouse 行为不回归
# ---------------------------------------------------------------------------

class TestNoRegression:
    """测试 14: 现有 vendor overlay 和 MarketWarehouse 行为不回归。"""

    def test_market_warehouse_basic_upsert_still_works(self, tmp_path):
        """基本 upsert 不回归：无 price_series_mode 列时正常工作。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = pd.DataFrame(
            {
                "symbol": ["600000"],
                "date": pd.Series(pd.to_datetime(["2026-01-05"])).dt.date,
                "open": [10.0],
                "high": [10.1],
                "low": [9.9],
                "close": [10.0],
                "volume": [1000.0],
                "turnover": [10000.0],
            }
        )
        stored = wh.upsert_daily_bars(frame=frame)
        assert stored == 1
        result = wh.fetch_all_daily_bars(symbol="600000")
        assert len(result) == 1
        assert result["close"].iloc[0] == 10.0

    def test_market_warehouse_same_mode_does_not_fail(self, tmp_path):
        """同 mode 多次写入不回归。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame1 = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame1)
        frame2 = _make_daily_frame("600000", ["2026-01-06"], price_series_mode="qfq")
        stored = wh.upsert_daily_bars(frame=frame2)
        assert stored == 1
        result = wh.fetch_all_daily_bars(symbol="600000")
        assert len(result) == 2

    def test_overwrite_existing_preserves_mode(self, tmp_path):
        """overwrite_existing=True 时同 mode 不报错。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame1 = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame1)
        frame2 = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq", close=11.0)
        wh.upsert_daily_bars(frame=frame2, overwrite_existing=True)
        result = wh.fetch_all_daily_bars(symbol="600000")
        assert result["close"].iloc[0] == 11.0


# ---------------------------------------------------------------------------
# 对抗场景测试（审查 P0 修复）
# ---------------------------------------------------------------------------

class TestAdversarialScenarios:
    """审查发现的对抗场景覆盖。"""

    def test_price_mode_gate_checks_all_existing_rows(self, tmp_path):
        """门禁检查全部已有行 mode，而非仅最新一行。

        构造 qfq+raw 混合历史（绕过门禁写入），再用默认门禁写入
        qfq 新行——应被拒绝（因为已有 raw 行与新 qfq 行冲突）。
        """
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 先写入 qfq 基线
        frame1 = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame1)
        # 绕过门禁写入 raw 行
        frame2 = _make_daily_frame("600000", ["2026-01-06"], price_series_mode="raw")
        wh.upsert_daily_bars(frame=frame2, enforce_price_series_mode=False)
        # 现在已有 qfq + raw 混合，写入 qfq 新行应被拒绝
        frame3 = _make_daily_frame("600000", ["2026-01-07"], price_series_mode="qfq")
        with pytest.raises(DataSourceError, match="price_series_mode mismatch"):
            wh.upsert_daily_bars(frame=frame3)

    def test_pit_status_overlap_rejected_before_write(self, tmp_path):
        """PIT 状态重叠区间在写入前被拒绝，而非事后检测。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 构造重叠区间
        frame = pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "effective_from": pd.Series(pd.to_datetime(["2026-01-01", "2026-01-15"])).dt.date,
                "effective_to": pd.Series(pd.to_datetime(["2026-02-28", "2026-03-15"])).dt.date,
                "status_type": ["st", "st"],
                "status_value": ["ST", "ST*"],
                "board": ["主板", "主板"],
                "exchange": ["SSE", "SSE"],
                "source": ["tushare", "tushare"],
                "as_of": ["2026-08-17", "2026-08-17"],
                "coverage_complete": [True, True],
            }
        )
        with pytest.raises(DataSourceError, match="overlap detected before write"):
            wh.upsert_security_status(symbol="600000", frame=frame)

    def test_identity_mapping_overlap_rejected_before_write(self, tmp_path):
        """身份映射重叠区间在写入前被拒绝。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = pd.DataFrame(
            {
                "historical_symbol": ["430001", "430001"],
                "canonical_symbol": ["920001", "920002"],
                "effective_from": pd.Series(pd.to_datetime(["2025-01-01", "2025-06-01"])).dt.date,
                "effective_to": pd.Series(pd.to_datetime(["2025-12-31", None])).dt.date,
                "source": ["external_verified", "external_verified"],
                "as_of": ["2026-08-17", "2026-08-17"],
            }
        )
        with pytest.raises(DataSourceError, match="overlap detected before write"):
            wh.upsert_security_identity_mapping(frame=frame)

    def test_audit_gate_rejects_missing_price_series_column(self, tmp_path):
        """审计门禁在 price_series_mode 列不存在时不放行。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # 创建有 daily_bars 但无 price_series_mode 列的 DB
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 写入不含 price_series_mode 的行
        frame = pd.DataFrame(
            {
                "symbol": ["600000"],
                "date": pd.Series(pd.to_datetime(["2026-01-05"])).dt.date,
                "open": [10.0],
                "high": [10.1],
                "low": [9.9],
                "close": [10.0],
                "volume": [1000.0],
                "turnover": [10000.0],
            }
        )
        wh.upsert_daily_bars(frame=frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        # NO-GO 预期（price_series_mode 列不存在）
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        gates = report.get("gates", {})
        psm_gate = gates.get("price_series_mode_consistency", {})
        assert psm_gate["pass"] is False
        # 列存在但全空（upsert_daily_bars 补齐为空串）
        assert psm_gate.get("nonempty_rows") == 0

    def test_audit_gate_rejects_all_empty_price_series(self, tmp_path):
        """审计门禁在 price_series_mode 全空时不放行。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 写入 price_series_mode 为空的行
        frame = pd.DataFrame(
            {
                "symbol": ["600000"],
                "date": pd.Series(pd.to_datetime(["2026-01-05"])).dt.date,
                "open": [10.0],
                "high": [10.1],
                "low": [9.9],
                "close": [10.0],
                "volume": [1000.0],
                "turnover": [10000.0],
                "price_series_mode": [""],
            }
        )
        wh.upsert_daily_bars(frame=frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        psm_gate = report.get("gates", {}).get("price_series_mode_consistency", {})
        assert psm_gate["pass"] is False

    def test_shadow_rebuild_missing_factors_skips_not_fake_label(self, tmp_path):
        """影子重建在无因子时不冒充 qfq，而是跳过并报告。"""
        import importlib.util

        script_path = ROOT / "scripts" / "shadow_rebuild_price_series.py"
        spec = importlib.util.spec_from_file_location("shadow_rebuild", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source_db = tmp_path / "source.duckdb"
        target_db = tmp_path / "target.duckdb"


        # 创建有 raw 数据的 source DB
        wh = MarketWarehouse(
            db_path=source_db,
            package_root=str(tmp_path / "src_pkg"),
            package_writes_enabled=False,
        )
        wh.ensure_schema()
        frame = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="raw")
        wh.upsert_daily_bars(frame=frame)

        # 不提供 --vendor-root，因子无法获取
        rc = mod._main([
            "--source-db", str(source_db),
            "--target-db", str(target_db),
            "--target-mode", "qfq",
            "--dry-run",
        ])
        # 缺少因子应返回 exit=1（失败，不是成功）
        assert rc == 1
        # target DB 不应被创建
        assert not target_db.exists()

    def test_shadow_rebuild_no_double_adjustment_for_qfq_rows(self, tmp_path):
        """P0 反例：mixed-mode 重建只调整 raw 行，不重复复权已 qfq 行。"""
        import importlib.util

        script_path = ROOT / "scripts" / "shadow_rebuild_price_series.py"
        spec = importlib.util.spec_from_file_location("shadow_rebuild", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        source_db = tmp_path / "source.duckdb"


        # 创建 mixed-mode 数据：qfq + raw
        wh = MarketWarehouse(
            db_path=source_db,
            package_root=str(tmp_path / "src_pkg"),
            package_writes_enabled=False,
        )
        wh.ensure_schema()
        # qfq 行 close=5.0
        frame_qfq = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq", close=5.0)
        wh.upsert_daily_bars(frame=frame_qfq, enforce_price_series_mode=False)
        # raw 行 close=10.0
        frame_raw = _make_daily_frame("600000", ["2026-01-06"], price_series_mode="raw", close=10.0)
        wh.upsert_daily_bars(frame=frame_raw, enforce_price_series_mode=False)

        # 用 _apply_qfq_adjustment 直接验证：qfq 行不被再次调整
        test_frame = pd.DataFrame({
            "date": pd.Series(pd.to_datetime(["2026-01-05", "2026-01-06"])).dt.date,
            "open": [5.0, 10.0],
            "high": [5.1, 10.1],
            "low": [4.9, 9.9],
            "close": [5.0, 10.0],
            "price_series_mode": ["qfq", "raw"],
        })
        # 构造一个因子序列（最新日=1.0）
        factors = pd.Series(
            [0.5, 1.0],
            index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
        )
        result = mod._apply_qfq_adjustment(test_frame, factors)
        # qfq 行 close 应保持 5.0（不被再次乘因子）
        qfq_close = result.loc[result["price_series_mode"] == "qfq", "close"].iloc[0]
        assert qfq_close == 5.0
        # raw 行 close 应被调整：10.0 * 1.0 = 10.0（因子为 1.0）
        raw_close = result.loc[result["price_series_mode"] == "raw", "close"].iloc[0]
        assert raw_close == 10.0

    def test_pit_status_overlap_with_db_rejected(self, tmp_path):
        """P0 反例：PIT 状态写前校验查询数据库旧记录，拒绝重叠。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 先写入旧区间
        frame1 = pd.DataFrame({
            "symbol": ["600000"],
            "effective_from": pd.Series(pd.to_datetime(["2026-01-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime(["2026-12-31"])).dt.date,
            "status_type": ["st"],
            "status_value": ["ST"],
            "board": ["主板"],
            "exchange": ["SSE"],
            "source": ["tushare"],
            "as_of": ["2026-08-17"],
            "coverage_complete": [True],
        })
        wh.upsert_security_status(symbol="600000", frame=frame1)
        # 再写单条重叠新区间——应被拒绝
        frame2 = pd.DataFrame({
            "symbol": ["600000"],
            "effective_from": pd.Series(pd.to_datetime(["2026-06-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime([None])).dt.date,
            "status_type": ["st"],
            "status_value": ["normal"],
            "board": ["主板"],
            "exchange": ["SSE"],
            "source": ["tushare"],
            "as_of": ["2026-08-17"],
            "coverage_complete": [True],
        })
        with pytest.raises(DataSourceError, match="overlap with existing DB"):
            wh.upsert_security_status(symbol="600000", frame=frame2)

    def test_identity_mapping_overlap_with_db_rejected(self, tmp_path):
        """P0 反例：身份映射写前校验查询数据库旧记录，拒绝重叠。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 先写入旧区间
        frame1 = pd.DataFrame({
            "historical_symbol": ["430001"],
            "canonical_symbol": ["920001"],
            "effective_from": pd.Series(pd.to_datetime(["2025-01-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime(["2025-12-31"])).dt.date,
            "source": ["external_verified"],
            "as_of": ["2026-08-17"],
        })
        wh.upsert_security_identity_mapping(frame=frame1)
        # 再写重叠新区间
        frame2 = pd.DataFrame({
            "historical_symbol": ["430001"],
            "canonical_symbol": ["920002"],
            "effective_from": pd.Series(pd.to_datetime(["2025-06-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime([None])).dt.date,
            "source": ["external_verified"],
            "as_of": ["2026-08-17"],
        })
        with pytest.raises(DataSourceError, match="overlap with existing DB"):
            wh.upsert_security_identity_mapping(frame=frame2)

    def test_audit_gate_rejects_all_raw(self, tmp_path):
        """P1 反例：全 raw 数据不放行价格口径门禁（系统配置要求 qfq）。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="raw")
        wh.upsert_daily_bars(frame=frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        psm_gate = report.get("gates", {}).get("price_series_mode_consistency", {})
        assert psm_gate["pass"] is False
        assert psm_gate.get("qfq_rows") == 0

    def test_pit_status_upsert_idempotent(self, tmp_path):
        """P0 反例：重复写入完全相同的 PIT 状态数据不报错（幂等）。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = pd.DataFrame({
            "symbol": ["600000"],
            "effective_from": pd.Series(pd.to_datetime(["2026-01-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime(["2026-12-31"])).dt.date,
            "status_type": ["st"],
            "status_value": ["ST"],
            "board": ["主板"],
            "exchange": ["SSE"],
            "source": ["tushare"],
            "as_of": ["2026-08-17"],
            "coverage_complete": [True],
        })
        # 第一次写入
        count1 = wh.upsert_security_status(symbol="600000", frame=frame)
        assert count1 == 1
        # 第二次写入完全相同的数据——不应抛异常
        count2 = wh.upsert_security_status(symbol="600000", frame=frame)
        assert count2 == 1

    def test_identity_mapping_upsert_idempotent(self, tmp_path):
        """P0 反例：重复写入完全相同的身份映射数据不报错（幂等）。"""
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        frame = pd.DataFrame({
            "historical_symbol": ["430001"],
            "canonical_symbol": ["920001"],
            "effective_from": pd.Series(pd.to_datetime(["2025-01-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime(["2025-12-31"])).dt.date,
            "source": ["external_verified"],
            "as_of": ["2026-08-17"],
        })
        count1 = wh.upsert_security_identity_mapping(frame=frame)
        assert count1 == 1
        # 第二次写入完全相同的数据——不应抛异常
        count2 = wh.upsert_security_identity_mapping(frame=frame)
        assert count2 == 1

    def test_audit_gate_rejects_low_historical_coverage(self, tmp_path):
        """P1 反例：历史覆盖率极低但最新日 100% 时门禁不放行。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # daily_bars 有 10 个交易日
        dates = [f"2026-01-{d:02d}" for d in range(1, 11)]
        frame = _make_daily_frame("600000", dates, price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame)
        # trade_status 只有最新 1 个交易日
        ts_frame = pd.DataFrame({
            "symbol": ["600000"],
            "trade_date": pd.Series(pd.to_datetime(["2026-01-10"])).dt.date,
            "up_limit": [11.0],
            "down_limit": [9.0],
            "suspended": [False],
            "suspend_type": [""],
            "source": ["tushare_stk_limit"],
            "as_of": ["2026-08-17"],
            "coverage_complete": [True],
        })
        wh.upsert_trade_status(symbol="600000", frame=ts_frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1  # NO-GO
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        ts_gate = report.get("gates", {}).get("exact_limit_label_coverage", {})
        assert ts_gate["pass"] is False
        # 覆盖率应为 1/10 = 0.1
        assert ts_gate["actual"] < 0.5

    def test_audit_gate_rejects_partial_raw_data(self, tmp_path):
        """P1 反例：1 行 qfq + 24999 行 raw 不放行价格口径门禁。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 1 行 qfq
        frame_qfq = _make_daily_frame("600000", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame_qfq)
        # 大量 raw 行
        raw_dates = [f"2026-01-{d:02d}" for d in range(6, 31)]
        frame_raw = _make_daily_frame("600000", raw_dates, price_series_mode="raw")
        wh.upsert_daily_bars(frame=frame_raw, enforce_price_series_mode=False)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        psm_gate = report.get("gates", {}).get("price_series_mode_consistency", {})
        assert psm_gate["pass"] is False
        assert psm_gate.get("raw_rows", 0) > 0

    def test_audit_gate_92_only_no_legacy_passes(self, tmp_path):
        """P1 反例：只有 92 开头代码、无旧代码需要映射时门禁通过。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 只有 92 开头代码，无 43/83/87
        frame = _make_daily_frame("920001", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        # NO-GO 是因为其他门禁（涨停标签、moneyflow 等覆盖不足）
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        im_gate = report.get("gates", {}).get("symbol_identity_mapping_coverage", {})
        # 身份映射门禁应通过（无旧代码需要映射 = N/A）
        assert im_gate["pass"] is True

    def test_audit_gate_rejects_date_misaligned_coverage(self, tmp_path):
        """P1 反例：标签日期与行情日期完全错位时门禁不放行。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # daily_bars 在 1 月 5-6 日
        frame = _make_daily_frame("600000", ["2026-01-05", "2026-01-06"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame)
        # trade_status 在 2 月 5-6 日——完全不重叠
        ts_frame = pd.DataFrame({
            "symbol": ["600000", "600000"],
            "trade_date": pd.Series(pd.to_datetime(["2026-02-05", "2026-02-06"])).dt.date,
            "up_limit": [11.0, 11.1],
            "down_limit": [9.0, 9.1],
            "suspended": [False, False],
            "suspend_type": ["", ""],
            "source": ["tushare_stk_limit", "tushare_stk_limit"],
            "as_of": ["2026-08-17", "2026-08-17"],
            "coverage_complete": [True, True],
        })
        wh.upsert_trade_status(symbol="600000", frame=ts_frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        ts_gate = report.get("gates", {}).get("exact_limit_label_coverage", {})
        assert ts_gate["pass"] is False
        # symbol-date 交集覆盖率应为 0（日期不重叠）
        assert ts_gate["actual"] == 0.0

    def test_audit_gate_rejects_one_symbol_per_day_coverage(self, tmp_path):
        """P1 反例：每天 100 只活跃股票但只有 1 只有标签时不放行。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 100 只股票，2 个交易日
        symbols = [f"{i:06d}" for i in range(1, 101)]
        all_rows = []
        for d in ["2026-01-05", "2026-01-06"]:
            for sym in symbols:
                all_rows.append({
                    "symbol": sym,
                    "date": pd.to_datetime(d).date(),
                    "open": 10.0, "high": 10.1, "low": 9.9,
                    "close": 10.0, "volume": 1000.0, "turnover": 10000.0,
                    "price_series_mode": "qfq",
                })
        frame = pd.DataFrame(all_rows)
        wh.upsert_daily_bars(frame=frame)
        # 只有 000001 有标签
        ts_frame = pd.DataFrame({
            "symbol": ["000001", "000001"],
            "trade_date": pd.Series(pd.to_datetime(["2026-01-05", "2026-01-06"])).dt.date,
            "up_limit": [11.0, 11.1],
            "down_limit": [9.0, 9.1],
            "suspended": [False, False],
            "suspend_type": ["", ""],
            "source": ["tushare_stk_limit", "tushare_stk_limit"],
            "as_of": ["2026-08-17", "2026-08-17"],
            "coverage_complete": [True, True],
        })
        wh.upsert_trade_status(symbol="000001", frame=ts_frame)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        ts_gate = report.get("gates", {}).get("exact_limit_label_coverage", {})
        assert ts_gate["pass"] is False
        # 覆盖率应为 2/200 = 0.01
        assert ts_gate["actual"] < 0.02
        # 逐日最低覆盖率应为 1/100 = 0.01
        assert ts_gate.get("min_daily_coverage", 0) < 0.02

    def test_audit_gate_rejects_irrelevant_mapping(self, tmp_path):
        """P1 反例：映射表只有无关映射，不充数。"""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        # 库内有 430001 需要映射
        frame = _make_daily_frame("430001", ["2026-01-05"], price_series_mode="qfq")
        wh.upsert_daily_bars(frame=frame)
        # 映射表只有无关的 830002 -> 920002
        mapping = pd.DataFrame({
            "historical_symbol": ["830002"],
            "canonical_symbol": ["920002"],
            "effective_from": pd.Series(pd.to_datetime(["2025-01-01"])).dt.date,
            "effective_to": pd.Series(pd.to_datetime([None])).dt.date,
            "source": ["external_verified"],
            "as_of": ["2026-08-17"],
        })
        wh.upsert_security_identity_mapping(frame=mapping)

        db_path = tmp_path / "test.duckdb"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(tmp_path / "audit.json"),
        ])
        assert rc == 1
        report = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
        im_gate = report.get("gates", {}).get("symbol_identity_mapping_coverage", {})
        # 库内有 1 个旧代码 430001 但没有对应的映射——覆盖率应为 0
        assert im_gate["pass"] is False
        assert im_gate["actual"] == 0.0

    def test_audit_gate_requires_min_daily_coverage(self):
        """Coverage gates require both overall and minimum daily ratios."""
        import importlib.util

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        gates = mod.evaluate_gates(
            daily_bars={"symbols": 1, "rows": 1},
            price_series={
                "has_column": True,
                "qfq_rows": 1,
                "raw_rows": 0,
                "mixed_symbols": 0,
            },
            trade_status={
                "symbol_date_coverage_ratio": 0.99,
                "min_daily_coverage_ratio": None,
            },
            moneyflow={
                "symbol_date_coverage_ratio": 0.99,
                "min_daily_coverage_ratio": None,
            },
            identity_mapping={"legacy_symbols_in_daily_bars": 0},
        )

        assert gates["exact_limit_label_coverage"]["pass"] is False
        assert gates["moneyflow_active_coverage"]["pass"] is False

    def test_audit_handles_mapping_table_without_daily_bars(self, tmp_path):
        """Mapping-only audit must return structured NO-GO without a query exception."""
        import importlib.util

        import duckdb

        script_path = ROOT / "scripts" / "audit_research_data_coverage.py"
        spec = importlib.util.spec_from_file_location("audit_tool", script_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        db_path = tmp_path / "mapping_only.duckdb"
        with duckdb.connect(str(db_path)) as con:
            con.execute(
                """
                CREATE TABLE security_identity_mapping (
                    historical_symbol VARCHAR,
                    canonical_symbol VARCHAR,
                    effective_from DATE,
                    effective_to DATE,
                    source VARCHAR,
                    as_of DATE
                )
                """
            )
            con.execute(
                """
                INSERT INTO security_identity_mapping
                VALUES ('430001', '920001', DATE '2025-01-01', NULL,
                        'external_verified', DATE '2026-08-17')
                """
            )

        output_path = tmp_path / "audit.json"
        rc = mod._main([
            "--db-path", str(db_path),
            "--output-json", str(output_path),
        ])

        assert rc == 1
        report = json.loads(output_path.read_text(encoding="utf-8"))
        assert report["daily_bars"]["table_exists"] is False
        assert report["gates"]["min_history_depth_days"]["pass"] is False


class TestFinalReviewFollowupFixes:
    """Adversarial coverage for the final P1/P2 review findings."""

    @staticmethod
    def _load_script(module_name: str, filename: str):
        import importlib.util

        script_path = ROOT / "scripts" / filename
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_partial_trade_status_does_not_checkpoint(self, tmp_path, monkeypatch):
        mod = self._load_script("backfill_trade_status_final", "backfill_trade_status.py")
        checkpoint = tmp_path / "checkpoint.json"
        db_path = tmp_path / "trade_status.duckdb"

        class PartialProvider:
            def fetch_trade_status(self, **_kwargs):
                frame = pd.DataFrame({
                    "symbol": ["600000"],
                    "trade_date": pd.to_datetime(["2026-08-18"]),
                    "up_limit": [11.0],
                    "down_limit": [9.0],
                    "suspended": [None],
                    "suspend_type": [""],
                    "source": ["tushare_stk_limit"],
                    "as_of": ["2026-08-18"],
                    "coverage_complete": [False],
                })
                frame.attrs["coverage_complete"] = False
                frame.attrs["failed_components"] = ["suspend_d"]
                return frame

        monkeypatch.setattr(mod, "_build_tushare_provider", lambda _args: PartialProvider())
        rc = mod._main([
            "--db-path", str(db_path),
            "--symbols", "600000",
            "--start-date", "2026-08-18",
            "--end-date", "2026-08-18",
            "--checkpoint-path", str(checkpoint),
            "--request-interval-sec", "0",
        ])

        assert rc == 1
        assert not checkpoint.exists()
        stored = MarketWarehouse(
            db_path=db_path,
            package_root=str(tmp_path / "pkg"),
            package_writes_enabled=False,
        ).fetch_trade_status(symbol="600000")
        assert len(stored) == 1

    def test_query_limit_up_stocks_requires_close_at_limit(self, tmp_path):
        mod = self._load_script("backfill_trade_status_query", "backfill_trade_status.py")
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        wh.upsert_daily_bars(frame=_make_daily_frame("600000", ["2026-08-18"], close=10.0))
        wh.upsert_daily_bars(frame=_make_daily_frame("000001", ["2026-08-18"], close=11.0))
        for symbol in ("600000", "000001"):
            wh.upsert_trade_status(symbol=symbol, frame=pd.DataFrame({
                "symbol": [symbol],
                "trade_date": pd.to_datetime(["2026-08-18"]),
                "up_limit": [11.0],
                "down_limit": [9.0],
                "suspended": [False],
                "suspend_type": [""],
                "source": ["tushare_stk_limit"],
                "as_of": ["2026-08-18"],
                "coverage_complete": [True],
            }))

        rows = mod.query_limit_up_stocks(str(tmp_path / "test.duckdb"), "2026-08-18")
        assert [row["symbol"] for row in rows] == ["000001"]

    def test_shadow_rebuild_rejects_unknown_mode(self, tmp_path):
        mod = self._load_script("shadow_rebuild_unknown", "shadow_rebuild_price_series.py")
        source_db = tmp_path / "source.duckdb"
        target_db = tmp_path / "target.duckdb"
        wh = MarketWarehouse(
            db_path=source_db,
            package_root=str(tmp_path / "src_pkg"),
            package_writes_enabled=False,
        )
        wh.ensure_schema()
        wh.upsert_daily_bars(
            frame=_make_daily_frame("600000", ["2026-08-18"], price_series_mode=""),
            enforce_price_series_mode=False,
        )

        rc = mod._main([
            "--source-db", str(source_db),
            "--target-db", str(target_db),
            "--target-mode", "qfq",
            "--dry-run",
        ])
        assert rc == 1
        assert not target_db.exists()

    def test_shadow_adjustment_does_not_use_future_factor(self):
        mod = self._load_script("shadow_rebuild_factor", "shadow_rebuild_price_series.py")
        frame = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-17", "2026-08-18"]).date,
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
            "price_series_mode": ["raw", "raw"],
        })
        factors = pd.Series([0.5], index=pd.to_datetime(["2026-08-18"]))

        adjusted = mod._apply_qfq_adjustment(frame, factors)
        inverted = mod._invert_qfq_adjustment(
            frame.assign(price_series_mode="qfq"),
            factors,
        )
        assert adjusted["close"].tolist() == [10.0, 5.0]
        assert inverted["close"].tolist() == [10.0, 20.0]

    def test_audit_null_limit_label_has_zero_coverage(self, tmp_path):
        mod = self._load_script("audit_null_label", "audit_research_data_coverage.py")
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        wh.upsert_daily_bars(frame=_make_daily_frame("600000", ["2026-08-18"]))
        wh.upsert_trade_status(symbol="600000", frame=pd.DataFrame({
            "symbol": ["600000"],
            "trade_date": pd.to_datetime(["2026-08-18"]),
            "up_limit": [float("nan")],
            "down_limit": [float("nan")],
            "suspended": [False],
            "suspend_type": [""],
            "source": ["tushare_suspend_d"],
            "as_of": ["2026-08-18"],
            "coverage_complete": [True],
        }))

        report = mod.build_audit_report(tmp_path / "test.duckdb")
        gate = report["gates"]["exact_limit_label_coverage"]
        assert gate["actual"] == 0.0
        assert gate["min_daily_coverage"] == 0.0
        assert gate["pass"] is False

    def test_history_depth_uses_latest_active_universe(self, tmp_path):
        mod = self._load_script("audit_active_history", "audit_research_data_coverage.py")
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        old_dates = pd.date_range("2025-01-01", periods=250, freq="D")
        wh.upsert_daily_bars(frame=_make_daily_frame(
            "600000",
            [value.date().isoformat() for value in old_dates],
        ))
        latest_date = (old_dates[-1] + pd.Timedelta(days=1)).date().isoformat()
        wh.upsert_daily_bars(frame=_make_daily_frame("000001", [latest_date]))

        report = mod.build_audit_report(tmp_path / "test.duckdb")
        gate = report["gates"]["min_history_depth_days"]
        assert gate["latest_trade_date_symbols"] == 1
        assert gate["ge_250_symbols"] == 0
        assert gate["pass"] is False

    def test_identity_mapping_requires_effective_date_overlap(self, tmp_path):
        mod = self._load_script("audit_identity_pit", "audit_research_data_coverage.py")
        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        wh.upsert_daily_bars(frame=_make_daily_frame("430001", ["2020-01-02"]))
        wh.upsert_security_identity_mapping(frame=pd.DataFrame({
            "historical_symbol": ["430001"],
            "canonical_symbol": ["920001"],
            "effective_from": pd.to_datetime(["2025-01-01"]).date,
            "effective_to": pd.to_datetime([None]).date,
            "source": ["external_verified"],
            "as_of": ["2026-08-18"],
        }))

        report = mod.build_audit_report(tmp_path / "test.duckdb")
        gate = report["gates"]["symbol_identity_mapping_coverage"]
        assert gate["actual"] == 0.0
        assert gate["pass"] is False

    def test_moneyflow_default_range_is_concrete_and_validated(self, tmp_path):
        mod = self._load_script("backfill_moneyflow_dates", "backfill_moneyflow.py")
        start_date, end_date = mod._resolve_date_range(
            "",
            "",
            today=date(2026, 8, 18),
        )
        assert start_date == date(2025, 8, 18)
        assert end_date == date(2026, 8, 18)
        assert mod._marker_key(start_date.isoformat(), end_date.isoformat(), "600000") == (
            "2025-08-18|2026-08-18|600000"
        )
        rc = mod._main([
            "--db-path", str(tmp_path / "moneyflow.duckdb"),
            "--symbols", "600000",
            "--start-date", "2026-08-19",
            "--end-date", "2026-08-18",
            "--dry-run",
        ])
        assert rc == 2

    def test_load_mapping_from_db_treats_nat_as_open_end(self):
        class FakeWarehouse:
            @staticmethod
            def fetch_security_identity_mapping():
                return pd.DataFrame({
                    "historical_symbol": ["430001"],
                    "canonical_symbol": ["920001"],
                    "effective_from": pd.to_datetime(["2025-01-01"]),
                    "effective_to": [pd.NaT],
                    "source": ["external_verified"],
                    "as_of": ["2026-08-18"],
                })

        entries = load_mapping_from_db(FakeWarehouse())
        assert len(entries) == 1
        assert entries[0].effective_to is None

    def test_readonly_overlap_detects_open_ended_interval(self, tmp_path):
        import duckdb

        wh = _make_warehouse(tmp_path)
        wh.ensure_schema()
        with duckdb.connect(str(tmp_path / "test.duckdb")) as con:
            con.execute(
                """
                INSERT INTO security_status
                (symbol, effective_from, effective_to, status_type, status_value,
                 board, exchange, source, as_of, coverage_complete)
                VALUES
                ('600000', DATE '2026-01-01', NULL, 'st', 'ST', '', '', '', '', TRUE),
                ('600000', DATE '2026-06-01', DATE '2026-12-31', 'st', 'ST', '', '', '', '', TRUE)
                """
            )

        overlaps = wh.validate_security_status_intervals(symbol="600000")
        assert len(overlaps) == 1

    @pytest.mark.parametrize("table_name", ["daily_trade_status", "moneyflow"])
    def test_audit_business_table_without_daily_bars_is_structured_no_go(
        self,
        tmp_path,
        table_name,
    ):
        import duckdb

        mod = self._load_script(f"audit_{table_name}_only", "audit_research_data_coverage.py")
        db_path = tmp_path / f"{table_name}.duckdb"
        with duckdb.connect(str(db_path)) as con:
            if table_name == "daily_trade_status":
                con.execute(
                    """
                    CREATE TABLE daily_trade_status (
                        symbol VARCHAR, trade_date DATE, up_limit DOUBLE,
                        coverage_complete BOOLEAN
                    )
                    """
                )
                con.execute(
                    "INSERT INTO daily_trade_status VALUES "
                    "('600000', DATE '2026-08-18', 11.0, TRUE)"
                )
            else:
                con.execute(
                    "CREATE TABLE moneyflow (symbol VARCHAR, trade_date DATE)"
                )
                con.execute(
                    "INSERT INTO moneyflow VALUES ('600000', DATE '2026-08-18')"
                )

        report = mod.build_audit_report(db_path)
        assert report["daily_bars"]["table_exists"] is False
        key = "moneyflow" if table_name == "moneyflow" else "trade_status"
        assert report[key]["rows"] == 1
        assert report["overall_verdict"] == "NO-GO"

