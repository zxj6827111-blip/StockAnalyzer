"""Phase 1 验收 smoke：真实 StockAnalyzerService 装配链路上手动触发 theme_daily_sync。

不打真实网络：fake akshare 模块注入 sys.modules（快讯/板块/期货三个接口）。
验证点（对应 M12 方案 Phase 1 验收标准）：
  1. service.run_theme_daily_sync() 返回 ok 报告；
  2. theme_state.json 合法产出（shadow dry-run：boost 表空 + pinned_pool 清单）；
  3. m12_theme_ledger.duckdb 写入事件；
  4. theme_news_latest.jsonl + theme_news_daily/YYYY-MM-DD.jsonl 双写。
运行：python scripts/theme_phase1_smoke.py
"""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _build_fake_akshare() -> types.ModuleType:
    fake = types.ModuleType("akshare")

    def stock_info_global_cls() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "发布日期": ["2026-09-09 07:30:00", "2026-09-09 08:00:00", "2026-09-09 08:10:00"],
                "标题": [
                    "中东地缘冲突升级 霍尔木兹海峡油轮遇袭",
                    "OPEC 宣布额外减产 原油供应收紧",
                    "某上市公司发布日常经营公告",
                ],
                "内容": ["局势紧张，油价走强", "减产幅度超预期", "无主题相关内容"],
            }
        )

    def stock_board_concept_cons_em(symbol: str) -> pd.DataFrame:
        if symbol in {"油气设服", "页岩气"}:
            return pd.DataFrame(
                {
                    "代码": ["600583", "002207", "605180"],
                    "名称": ["海油工程", "准油股份", "华电重工"],
                }
            )
        raise RuntimeError("concept not found")

    def stock_board_industry_cons_em(symbol: str) -> pd.DataFrame:
        if symbol == "石油行业":
            return pd.DataFrame({"代码": ["600028", "601857"], "名称": ["中石化", "中石油"]})
        raise RuntimeError("industry not found")

    def futures_zh_daily_sina(symbol: str) -> pd.DataFrame:
        if symbol == "SC0":
            return pd.DataFrame(
                {
                    "date": ["2026-09-04", "2026-09-05", "2026-09-08", "2026-09-09"],
                    "close": [500.0, 504.0, 509.0, 522.0],
                }
            )
        if symbol == "B0":
            return pd.DataFrame({"date": ["2026-09-08", "2026-09-09"], "close": [80.0, 80.3]})
        raise RuntimeError(f"unknown contract {symbol}")

    fake.stock_info_global_cls = stock_info_global_cls
    fake.stock_board_concept_cons_em = stock_board_concept_cons_em
    fake.stock_board_industry_cons_em = stock_board_industry_cons_em
    fake.futures_zh_daily_sina = futures_zh_daily_sina
    return fake


def main() -> int:
    import os
    import tempfile

    # 落盘全部指向临时目录，避免污染仓库 artifacts
    tmp_root = Path(tempfile.mkdtemp(prefix="theme_phase1_smoke_"))
    os.environ["SA__THEME__ENABLED"] = "true"
    os.environ["SA__THEME__STATE_PATH"] = str(tmp_root / "theme_state.json")
    os.environ["SA__THEME__LEDGER_DB_PATH"] = str(tmp_root / "m12_theme_ledger.duckdb")
    os.environ["SA__THEME__LEDGER_ARCHIVE_DIR"] = str(tmp_root / "m12_archive")
    os.environ["SA__THEME__NEWS_LATEST_PATH"] = str(tmp_root / "theme_news_latest.jsonl")
    os.environ["SA__THEME__NEWS_DAILY_DIR"] = str(tmp_root / "theme_news_daily")
    os.environ["SA__THEME__REVIEW_PATH"] = str(tmp_root / "theme_review.jsonl")
    os.environ["STOCK_ANALYZER_CONFIG"] = str(PROJECT_ROOT / "config" / "default.yaml")

    original_ak = sys.modules.get("akshare")
    sys.modules["akshare"] = _build_fake_akshare()
    try:
        from stock_analyzer.config import load_config
        from stock_analyzer.runtime.service import StockAnalyzerService

        config = load_config()
        assert config.theme.mode == "shadow", f"theme.mode 应为 shadow，实际 {config.theme.mode}"
        service = StockAnalyzerService(config)
        # 注入 fake akshare 给 theme 抓取链路（服务构造时已缓存，注入属性即可）
        service._theme_ak_module = sys.modules["akshare"]

        # 2026-09-07 周一（交易日），16:45 本地时区触发
        report = service.run_theme_daily_sync(
            timestamp=datetime(2026, 9, 7, 16, 45, tzinfo=UTC)
        )
        checks: list[tuple[str, bool, str]] = []
        checks.append(("sync status ok", report.get("status") == "ok", str(report.get("status"))))
        checks.append(("mode shadow", report.get("mode") == "shadow", str(report.get("mode"))))
        checks.append(("records==3", report.get("records") == 3, str(report.get("records"))))
        checks.append(
            (
                "extractions==2 (geo_oil)",
                report.get("extractions") == 2,
                str(report.get("extractions")),
            )
        )
        checks.append(
            ("geo_oil price confirmed", report.get("active_themes") == ["geo_oil"],
             str(report.get("active_themes")))
        )

        state_path = tmp_root / "theme_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        checks.append(("theme_state.json exists", bool(state), str(state_path)))
        checks.append(("state dry_run", state.get("dry_run") is True, str(state.get("dry_run"))))
        checks.append(
            ("boost table empty (shadow)", state.get("boost_by_symbol") == {},
             str(state.get("boost_by_symbol")))
        )
        pool = state.get("pinned_pool", [])
        checks.append(
            ("pinned_pool dry-run 清单非空", isinstance(pool, list) and 0 < len(pool) <= 10,
             str(pool))
        )

        ledger_path = tmp_root / "m12_theme_ledger.duckdb"
        checks.append(("ledger written", ledger_path.exists(), str(ledger_path)))
        latest = tmp_root / "theme_news_latest.jsonl"
        daily = tmp_root / "theme_news_daily" / "2026-09-07.jsonl"
        checks.append(("news latest written", latest.exists(), str(latest)))
        checks.append(("news daily archive written", daily.exists(), str(daily)))

        events = service.theme_events(limit=10)
        checks.append(
            ("theme_events records==2", events.get("records") == 2, str(events.get("records")))
        )
        readiness = service.theme_shadow_readiness()
        checks.append(
            ("shadow readiness not ready", readiness.get("ready_for_boost") is False,
             json.dumps(readiness, ensure_ascii=False)[:80])
        )
    finally:
        if original_ak is None:
            sys.modules.pop("akshare", None)
        else:
            sys.modules["akshare"] = original_ak

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    print(f"\nsmoke root: {tmp_root}")
    print("RESULT:", "ALL PASS" if not failed else f"{len(failed)} FAILED")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
