"""M12 主题层编排（RuntimeThemeService）+ pipeline 分量接入测试。

用 fake 宿主 service（duckdb tmp_path + fake akshare）端到端验证：
theme_daily_sync → theme_state.json → ThemeBoostProvider 消费，
以及 pipeline components 条件加入 theme_boost（照 news 先例）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.config import MacroThemeConfig, StockAnalyzerConfig, load_config
from stock_analyzer.runtime.services.theme_service import RuntimeThemeService
from stock_analyzer.theme.scorer import ThemeBoostProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeEvolutionCore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _resolve_evolution_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return candidate
        return self._root / candidate


class _FakeService:
    """theme_service 依赖的最小宿主契约。"""

    def __init__(self, config: StockAnalyzerConfig, root: Path, ak_module: object) -> None:
        self._config = config
        self._theme_ak_module = ak_module
        self._evolution_core_service = _FakeEvolutionCore(root)
        self.audit_events: list[dict[str, object]] = []

    def _record_audit_event(
        self,
        *,
        event_type: str,
        payload: dict[str, object] | None = None,
        level: str = "info",
        message: str = "",
    ) -> None:
        self.audit_events.append(
            {"event_type": event_type, "payload": payload or {}, "level": level}
        )

    def _resolve_evolution_path(self, raw_path: str) -> Path:
        # 真实宿主是转发到 evolution_core_service，这里同构委托
        return self._evolution_core_service._resolve_evolution_path(raw_path)


class _ThemeAkFake:
    """宏观快讯 + 期货日线 fake（SC 原油近 1 日 +2.5%，超 1.5% 阈值）。"""

    def stock_info_global_cls(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "发布日期": ["2026-09-09 07:30:00", "2026-09-09 08:00:00", "2026-09-09 08:10:00"],
                "标题": [
                    "中东地缘冲突升级 霍尔木兹海峡油轮遇袭",
                    "OPEC 宣布额外减产 原油供应收紧",
                    "某公司发布日常经营公告",
                ],
                "内容": ["局势紧张，油价走强", "减产幅度超预期", "无主题相关内容"],
            }
        )

    def stock_board_concept_cons_em(self, symbol: str) -> pd.DataFrame:
        if symbol in {"油气设服", "页岩气"}:
            return pd.DataFrame(
                {"代码": ["600583", "002207"], "名称": ["海油工程", "准油股份"]}
            )
        raise RuntimeError("concept not found")

    def stock_board_industry_cons_em(self, symbol: str) -> pd.DataFrame:
        if symbol == "石油行业":
            return pd.DataFrame({"代码": ["600028", "601857"], "名称": ["中石化", "中石油"]})
        raise RuntimeError("industry not found")

    def futures_zh_daily_sina(self, symbol: str) -> pd.DataFrame:
        if symbol == "SC0":
            return pd.DataFrame(
                {
                    "date": ["2026-09-04", "2026-09-05", "2026-09-08", "2026-09-09"],
                    "close": [500.0, 504.0, 509.0, 522.0],
                }
            )
        if symbol == "B0":
            return pd.DataFrame(
                {
                    "date": ["2026-09-08", "2026-09-09"],
                    "close": [80.0, 80.3],
                }
            )
        raise RuntimeError(f"unknown contract {symbol}")


@pytest.fixture
def fake_service(tmp_path: Path) -> _FakeService:
    config = load_config()
    # 落盘路径全部指到 tmp（避免污染仓库 artifacts）；model_copy 保别名不重验证
    theme = MacroThemeConfig(
        mode="shadow",
        enabled=True,
        ledger_db_path=str(tmp_path / "m12_theme_ledger.duckdb"),
        ledger_archive_dir=str(tmp_path / "m12_archive"),
        state_path=str(tmp_path / "theme_state.json"),
        news_latest_path=str(tmp_path / "theme_news_latest.jsonl"),
        news_daily_dir=str(tmp_path / "theme_news_daily"),
        review_path=str(tmp_path / "theme_review.jsonl"),
    )
    config = config.model_copy(
        update={"theme": theme},
    )
    return _FakeService(config, tmp_path, _ThemeAkFake())


def _trading_monday() -> datetime:
    # 2026-09-07 是周一（交易日）；13:30 UTC = 21:30 北京，夜间扫描前
    return datetime(2026, 9, 7, 13, 30, tzinfo=UTC)


def test_theme_daily_sync_end_to_end_shadow(fake_service: _FakeService, tmp_path: Path) -> None:
    service = RuntimeThemeService(fake_service)
    report = cast(dict[str, object], service.run_theme_daily_sync(timestamp=_trading_monday()))

    assert report["status"] == "ok"
    assert report["mode"] == "shadow"
    assert report["records"] == 3
    # 两条 geo_oil 快讯（冲突/减产）+ 一条无关
    assert report["extractions"] == 2
    # SC 原油 +2.5% 超 1.5% 阈值 → geo_oil 价格确认激活
    assert report["active_themes"] == ["geo_oil"]

    steps = cast(dict[str, object], report["steps"])
    themes = cast(list[dict[str, object]], steps["themes"])
    geo = next(t for t in themes if t["theme_id"] == "geo_oil")
    assert geo["price_confirmed"] is True
    assert "SC原油" in cast(list[str], geo["confirmed_by"])
    assert geo["heat"] > 0.0

    # theme_state.json 产出且为 shadow dry-run：boost 表空、pinned_pool 记清单
    state_path = tmp_path / "theme_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["mode"] == "shadow"
    assert state["dry_run"] is True
    assert state["active_themes"] == ["geo_oil"]
    assert state["boost_by_symbol"] == {}
    # dry-run 注入清单 = 板块成分（石油行业 + 油气设服 + 页岩气... 截断到 pinned_max）
    assert len(state["pinned_pool"]) > 0
    assert len(state["pinned_pool"]) <= 10

    # 快讯双写：滚动 + 按日归档
    latest = tmp_path / "theme_news_latest.jsonl"
    daily = tmp_path / "theme_news_daily" / "2026-09-07.jsonl"
    assert latest.exists() and daily.exists()

    # 账本写入
    ledger_db = tmp_path / "m12_theme_ledger.duckdb"
    assert ledger_db.exists()
    events = cast(dict[str, object], service.theme_events(limit=10))
    assert cast(int, events["records"]) == 2

    # shadow 模式下 boost provider 恒 0（评分零污染）
    provider = ThemeBoostProvider(config=fake_service._config.theme)
    assert provider.build_boost_map() == {}
    assert provider.available("600028") is False

    # 审计事件
    assert any(e["event_type"] == "theme_daily_sync" for e in fake_service.audit_events)


def test_theme_daily_sync_skips_non_trading_day(fake_service: _FakeService) -> None:
    service = RuntimeThemeService(fake_service)
    # 2026-09-05 周六
    report = cast(
        dict[str, object],
        service.run_theme_daily_sync(timestamp=datetime(2026, 9, 5, 8, 0, tzinfo=UTC)),
    )
    assert report["status"] == "skipped"
    assert report["reason"] == "not_a_trading_day"


def test_theme_daily_sync_off_mode_short_circuits(fake_service: _FakeService) -> None:
    fake_service._config = fake_service._config.model_copy(
        update={"theme": fake_service._config.theme.model_copy(update={"mode": "off"})}
    )
    service = RuntimeThemeService(fake_service)
    report = cast(dict[str, object], service.run_theme_daily_sync(timestamp=_trading_monday()))
    assert report["status"] == "skipped"
    assert report["reason"] == "theme_mode_off"


def test_theme_daily_sync_is_idempotent_same_day(fake_service: _FakeService) -> None:
    service = RuntimeThemeService(fake_service)
    first = cast(dict[str, object], service.run_theme_daily_sync(timestamp=_trading_monday()))
    second = cast(dict[str, object], service.run_theme_daily_sync(timestamp=_trading_monday()))
    assert first["status"] == second["status"] == "ok"
    # 账本 dedup：同日重跑不重复插入
    first_ingest = cast(
        dict[str, object], cast(dict[str, object], first["steps"])["ledger"]
    )
    second_ingest = cast(
        dict[str, object], cast(dict[str, object], second["steps"])["ledger"]
    )
    assert cast(int, first_ingest["inserted"]) >= 1
    assert cast(int, second_ingest["inserted"]) == 0
    # 归档幂等：同日文件合并去重重写
    daily = tmp_daily_path(fake_service)
    lines = [json.loads(line) for line in daily.read_text(encoding="utf-8").splitlines() if line]
    ids = [str(item.get("id") or item.get("title")) for item in lines]
    assert len(ids) == len(set(ids))


def tmp_daily_path(fake_service: _FakeService) -> Path:
    daily_dir = fake_service._config.theme.news_daily_dir
    path = Path(daily_dir)
    if not path.is_absolute():
        return Path("nonexistent-placeholder")
    return path / "2026-09-07.jsonl"


def test_theme_shadow_readiness_gates(fake_service: _FakeService, tmp_path: Path) -> None:
    service = RuntimeThemeService(fake_service)
    readiness = cast(dict[str, object], service.theme_shadow_readiness())
    # 无数据 → not ready
    assert readiness["ready_for_boost"] is False
    assert readiness["trade_days"] == 0

    # 人工复核一致率：写 review.jsonl（3 条 2 同意 → 0.667 < 0.80 不达标）
    review_path = tmp_path / "theme_review.jsonl"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(
        "\n".join(
            json.dumps({"theme_id": "geo_oil", "title": t, "agree": a})
            for t, a in [("冲突油价", True), ("高温电力", True), ("无关报道", False)]
        ),
        encoding="utf-8",
    )
    readiness = cast(dict[str, object], service.theme_shadow_readiness())
    assert cast(float | None, readiness["human_agreement_rate"]) == pytest.approx(2 / 3)


def test_pipeline_theme_component_with_fake_provider() -> None:
    """pipeline components 条件加入 theme_boost（照 news 先例）。

    用最小 fake provider 直接验证 _score_theme_boost_component 契约：
    available=False → (0, False) 不加键；available=True → 分量进 components。
    """
    from stock_analyzer.pipeline import AnalyzerPipeline

    config = load_config()
    pipeline = AnalyzerPipeline(config=config)
    # 默认 Neutral provider：恒 (0.0, False)
    value, available = pipeline._score_theme_boost_component(
        symbol="600028",
        bars=cast(Any, pd.DataFrame()),
        features=cast(Any, pd.DataFrame()),
        strategy="trend",
    )
    assert (value, available) == (0.0, False)

    class _FakeProvider:
        def available(self, symbol: str = "") -> bool:
            return symbol == "600028"

        def score(
            self,
            *,
            symbol: str,
            bars: Any,
            features: Any,
            strategy: str,
        ) -> float:
            return 0.6

    pipeline2 = AnalyzerPipeline(config=config, theme_boost_provider=cast(Any, _FakeProvider()))
    value2, available2 = pipeline2._score_theme_boost_component(
        symbol="600028",
        bars=cast(Any, pd.DataFrame()),
        features=cast(Any, pd.DataFrame()),
        strategy="trend",
    )
    assert (value2, available2) == (0.6, True)


def test_score_engine_theme_boost_weight_zero_neutralizes() -> None:
    """权重 0 时 theme_boost 进 components 不改总分（零污染结构保障）。"""
    from stock_analyzer.signal.scoring import ScoreEngine

    config = load_config()
    engine = ScoreEngine(config)
    base = engine.score(
        components={"lgbm": 0.6, "xgb": 0.5, "meta": 0.5, "board": 0.5, "completion": 0.5},
        strategy="trend",
    )
    with_theme = engine.score(
        components={
            "lgbm": 0.6,
            "xgb": 0.5,
            "meta": 0.5,
            "board": 0.5,
            "completion": 0.5,
            "theme_boost": 1.0,
        },
        strategy="trend",
    )
    assert base.total_score == pytest.approx(with_theme.total_score, abs=1e-9)
