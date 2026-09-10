"""M12 主题层（Macro Theme Layer）单元测试。

照 M7 测试模式：纯函数内联输入 + tmp_path 落盘 + fake akshare 模块注入，
不打真实网络。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.config import MacroThemeConfig, StockAnalyzerConfig, load_config
from stock_analyzer.theme.board_resolver import BoardResolver
from stock_analyzer.theme.extractor import (
    extract_theme_events,
    extract_theme_events_from_records,
    theme_heat,
)
from stock_analyzer.theme.ledger import ThemeEventLedger
from stock_analyzer.theme.macro_news_adapter import (
    MacroNewsAdapter,
    _normalize_published_at,
)
from stock_analyzer.theme.price_confirmation import PriceConfirmationAdapter
from stock_analyzer.theme.scorer import (
    NeutralThemeBoostProvider,
    ThemeBoostProvider,
    build_theme_pool,
)
from stock_analyzer.theme.taxonomy import ThemeConfirmation, load_taxonomy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_PATH = PROJECT_ROOT / "config" / "theme_taxonomy.yaml"


@pytest.fixture
def taxonomy() -> Any:
    return load_taxonomy(TAXONOMY_PATH)



def _score(provider: Any, symbol: str) -> float:
    return provider.score(
        symbol=symbol,
        bars=cast(Any, pd.DataFrame()),
        features=cast(Any, pd.DataFrame()),
        strategy="trend",
    )


# ---------------------------------------------------------------------------
# taxonomy
# ---------------------------------------------------------------------------


def test_seed_taxonomy_loads_and_is_valid() -> None:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    assert len(taxonomy.themes) <= 6, "种子知识库必须先小而准（≤6 族）"
    assert all(theme.commodities for theme in taxonomy.themes)
    assert taxonomy.confirmation.price_move_1d_min == 0.015
    assert taxonomy.confirmation.price_move_3d_min == 0.03


def test_taxonomy_rejects_duplicate_theme_id(tmp_path: Path) -> None:
    raw = """
themes:
  - theme_id: dup
    event_type: policy
    event_keywords: [关键词]
    direction: 1
    commodities: [螺纹钢]
    boards: [水泥建材]
  - theme_id: dup
    event_type: policy
    event_keywords: [关键词]
    direction: 1
    commodities: [螺纹钢]
    boards: [水泥建材]
"""
    path = tmp_path / "bad.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate theme_id"):
        load_taxonomy(path)


def test_taxonomy_rejects_commodityless_theme(tmp_path: Path) -> None:
    raw = """
themes:
  - theme_id: no_commodity
    event_type: policy
    event_keywords: [关键词]
    direction: 1
    commodities: []
    boards: [水泥建材]
"""
    path = tmp_path / "bad.yaml"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="empty commodities"):
        load_taxonomy(path)


# ---------------------------------------------------------------------------
# extractor
# ---------------------------------------------------------------------------


def test_extractor_title_hit_yields_high_intensity(taxonomy: Any) -> None:
    extractions = extract_theme_events(
        taxonomy=taxonomy,
        title="霍尔木兹海峡油轮遭袭击，中东局势升级",
    )
    assert len(extractions) == 1
    item = extractions[0]
    assert item.theme_id == "geo_oil"
    assert item.event_type == "geopolitics"
    assert item.direction == 1
    assert item.intensity == 1.0
    assert 0.0 < item.confidence <= 1.0


def test_extractor_content_multi_hit_medium(taxonomy: Any) -> None:
    extractions = extract_theme_events(
        taxonomy=taxonomy,
        title="全国天气周报",
        content="南方持续干旱，多地发布寒潮预警",
    )
    hits = {e.theme_id: e for e in extractions}
    assert "enso_agri" in hits
    agri = hits["enso_agri"]
    assert agri.intensity == 0.7
    assert "干旱" in agri.matched_keywords


def test_extractor_no_match_returns_empty(taxonomy: Any) -> None:
    assert extract_theme_events(taxonomy=taxonomy, title="公司年报披露") == []


def test_extractor_one_event_can_hit_multiple_themes(taxonomy: Any) -> None:
    # 战争（geo_oil）+ 断供（supply_chip）双主题
    extractions = extract_theme_events(
        taxonomy=taxonomy,
        title="出口管制与战争风险并行，芯片断供加剧油价扰动",
    )
    theme_ids = {e.theme_id for e in extractions}
    assert "geo_oil" in theme_ids
    assert "supply_chip" in theme_ids


def test_theme_heat_scales_with_unique_titles(taxonomy: Any) -> None:
    extractions = []
    for i in range(4):
        extractions.extend(
            extract_theme_events(
                taxonomy=taxonomy,
                title=f"第{i}条：某地冲突升级油价承压",
                content="同一主题转载报道",
            )
        )
    # 4 个不同标题 → 0.8；标题去重（同一标题多次转载不重复计热度）
    assert theme_heat(extractions) == 0.8
    single = extract_theme_events(taxonomy=taxonomy, title="唯一一条高温预警")
    assert theme_heat(single) == 0.4


def test_extract_from_records_batch(taxonomy: Any) -> None:
    records = [
        {"title": "极端高温红色预警", "content": ""},
        {"title": "某公司日常公告", "content": ""},
    ]
    extractions = extract_theme_events_from_records(taxonomy=taxonomy, records=records)
    assert {e.theme_id for e in extractions} == {"heat_power"}


# ---------------------------------------------------------------------------
# macro_news_adapter（fake akshare 注入）
# ---------------------------------------------------------------------------


class _FakeAkshare:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.calls = 0

    def stock_info_global_cls(self) -> pd.DataFrame:
        self.calls += 1
        return self._frame


def test_macro_news_adapter_normalizes_cls_frame() -> None:
    frame = pd.DataFrame(
        {
            "发布日期": ["2026-09-09 10:00:00", "2026-09-09 09:00:00"],
            "标题": ["地缘冲突升级", "每日晨报"],
            "内容": ["中东局势紧张", "市场概览"],
            "链接": ["http://x/1", "http://x/2"],
        }
    )
    ak = _FakeAkshare(frame)
    adapter = MacroNewsAdapter(provider="cls", ak_module=ak)
    records = adapter.fetch_latest()
    assert len(records) == 2
    assert records[0].title == "地缘冲突升级"
    assert records[0].published_at == "2026-09-09 10:00:00"
    # TTL 缓存：第二次调用不打接口
    adapter.fetch_latest()
    assert ak.calls == 1


def test_macro_news_adapter_raises_on_missing_module() -> None:
    class _Broken:
        pass

    adapter = MacroNewsAdapter(provider="cls", ak_module=None)
    # 无 akshare 时 fetch_latest 抛 DataSourceError（sys.modules 注入见下一测试）
    import pytest as _pytest

    original = sys.modules.get("akshare")
    sys.modules["akshare"] = cast(Any, ModuleType("akshare"))
    try:
        with _pytest.raises(Exception, match="not callable"):
            adapter.fetch_latest(force_refresh=True)
    finally:
        if original is None:
            sys.modules.pop("akshare", None)
        else:
            sys.modules["akshare"] = original


def test_macro_news_adapter_falls_back_to_em_on_cls_failure() -> None:
    """主源 cls 失败自动降级 em（方案风险项 1 缓解措施）。"""

    class _ClsDownEmOk:
        def stock_info_global_cls(self) -> pd.DataFrame:
            raise RuntimeError("cls down")

        def stock_info_global_em(self) -> pd.DataFrame:
            return pd.DataFrame(
                {"发布时间": ["2026-09-09 10:00:00"], "标题": ["东财快讯"], "内容": ["x"]}
            )

    adapter = MacroNewsAdapter(provider="cls", ak_module=_ClsDownEmOk())
    records = adapter.fetch_latest(force_refresh=True)
    assert len(records) == 1
    assert records[0].title == "东财快讯"
    assert adapter.last_provider == "em"


def test_macro_news_adapter_raises_when_all_providers_fail() -> None:
    from stock_analyzer.data.provider import DataSourceError

    class _AllDown:
        def stock_info_global_cls(self) -> pd.DataFrame:
            raise RuntimeError("cls down")

        def stock_info_global_em(self) -> pd.DataFrame:
            raise RuntimeError("em down")

    adapter = MacroNewsAdapter(provider="cls", ak_module=_AllDown())
    with pytest.raises(DataSourceError, match="all providers failed"):
        adapter.fetch_latest(force_refresh=True)


def test_normalize_published_at_iso() -> None:
    assert _normalize_published_at("2026-09-09 10:00:00") == "2026-09-09T10:00:00"
    assert _normalize_published_at("garbage") == "garbage"


# ---------------------------------------------------------------------------
# price_confirmation
# ---------------------------------------------------------------------------


class _FuturesFake:
    """futures_zh_daily_sina fake：SC0 拉涨 2.5%（>1.5% 阈值）。"""

    def futures_zh_daily_sina(self, symbol: str) -> pd.DataFrame:
        if symbol == "SC0":
            return pd.DataFrame(
                {
                    "date": ["2026-09-04", "2026-09-05", "2026-09-08", "2026-09-09"],
                    "close": [500.0, 505.0, 510.0, 523.0],
                }
            )
        if symbol == "SR0":
            return pd.DataFrame(
                {
                    "date": ["2026-09-04", "2026-09-05", "2026-09-08", "2026-09-09"],
                    "close": [6000.0, 6005.0, 6010.0, 6015.0],
                }
            )
        raise RuntimeError(f"unknown contract: {symbol}")


def test_price_confirmation_confirms_threshold_break() -> None:
    adapter = PriceConfirmationAdapter(
        confirmation=ThemeConfirmation(price_move_1d_min=0.015, price_move_3d_min=0.03),
        ak_module=_FuturesFake(),
    )
    result = adapter.confirm_commodity("SC原油")
    assert result.status == "ok"
    assert result.move_1d is not None and result.move_1d > 0.015
    assert result.confirmed is True


def test_price_confirmation_rejects_flat_price() -> None:
    adapter = PriceConfirmationAdapter(
        confirmation=ThemeConfirmation(),
        ak_module=_FuturesFake(),
    )
    result = adapter.confirm_commodity("白糖")  # SR0 近 1 日 +0.08%
    assert result.confirmed is False


def test_price_confirmation_unresolved_commodity() -> None:
    adapter = PriceConfirmationAdapter(ak_module=_FuturesFake())
    result = adapter.confirm_commodity("不存在的商品")
    assert result.status == "unresolved"
    assert result.confirmed is False


def test_price_confirmation_fetch_fail_is_unconfirmed() -> None:
    class _BrokenAk:
        def futures_zh_daily_sina(self, symbol: str) -> pd.DataFrame:
            raise RuntimeError("network down")

    adapter = PriceConfirmationAdapter(ak_module=_BrokenAk())
    result = adapter.confirm_commodity("SC原油")
    assert result.status == "unconfirmed"
    assert result.confirmed is False


def test_price_confirmation_theme_level_any_commodity() -> None:
    adapter = PriceConfirmationAdapter(ak_module=_FuturesFake())
    result = adapter.confirm_theme(
        theme_id="geo_oil",
        commodities=["SC原油", "白糖"],
        direction=1,
    )
    assert result.confirmed is True
    assert "SC原油" in result.confirmed_by
    assert "白糖" not in result.confirmed_by


def test_price_confirmation_direction_negative() -> None:
    class _DownFake:
        def futures_zh_daily_sina(self, symbol: str) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "date": ["2026-09-08", "2026-09-09"],
                    "close": [100.0, 95.0],
                }
            )

    adapter = PriceConfirmationAdapter(ak_module=_DownFake())
    result = adapter.confirm_theme(theme_id="x", commodities=["黄金"], direction=-1)
    assert result.confirmed is True  # -5% 跌幅 + direction=-1 → 方向一致


# ---------------------------------------------------------------------------
# board_resolver
# ---------------------------------------------------------------------------


class _BoardFake:
    def __init__(self) -> None:
        self.calls = 0

    def stock_board_concept_cons_em(self, symbol: str) -> pd.DataFrame:
        self.calls += 1
        if symbol != "虚拟电厂":
            raise RuntimeError("concept board not found")
        return pd.DataFrame(
            {"代码": ["600011", "600027", "600886"], "名称": ["华能", "国电", "国投"]}
        )

    def stock_board_industry_cons_em(self, symbol: str) -> pd.DataFrame:
        raise RuntimeError("industry board not found")


def test_board_resolver_caches_per_day(tmp_path: Path) -> None:
    ak = _BoardFake()
    resolver = BoardResolver(cache_dir=tmp_path, ak_module=ak)
    result = resolver.resolve("虚拟电厂")
    assert result.symbols == ["600011", "600027", "600886"]
    assert result.source == "concept"
    resolver.resolve("虚拟电厂")
    assert ak.calls == 1  # 当日文件缓存命中


def test_board_resolver_falls_back_to_stale_cache(tmp_path: Path) -> None:
    # 第一天：正常拉取写缓存
    ak = _BoardFake()
    resolver = BoardResolver(cache_dir=tmp_path, ak_module=ak)
    resolver.resolve("虚拟电厂", today=datetime(2026, 9, 7).date())
    # 第二天：接口挂了 → 降级沿用上日缓存（stale）
    class _Broken:
        def stock_board_concept_cons_em(self, symbol: str) -> pd.DataFrame:
            raise RuntimeError("down")

        def stock_board_industry_cons_em(self, symbol: str) -> pd.DataFrame:
            raise RuntimeError("down")

    resolver2 = BoardResolver(cache_dir=tmp_path, ak_module=_Broken())
    result = resolver2.resolve("虚拟电厂", today=datetime(2026, 9, 8).date())
    assert result.stale is True
    assert result.source == "stale_cache"
    assert result.symbols == ["600011", "600027", "600886"]


# ---------------------------------------------------------------------------
# ledger（tmp_path + 真 duckdb）
# ---------------------------------------------------------------------------


def test_theme_ledger_ingest_dedup_and_effectiveness(tmp_path: Path) -> None:
    ledger = ThemeEventLedger(
        db_path=tmp_path / "m12_theme_ledger.duckdb",
        archive_dir=tmp_path / "archive",
        ttl_days=14,
    )
    now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    records = [
        {
            "theme_id": "geo_oil",
            "event_type": "geopolitics",
            "direction": 1,
            "headline": "中东冲突油价跳涨",
            "matched_keywords": ["冲突"],
            "intensity": 1.0,
            "confidence": 0.8,
            "price_confirmed": True,
            "confirmed_by": "SC原油",
            "published_at": "2026-09-09T07:00:00+00:00",
        },
        # 同主题同日同标题 → dedup
        {
            "theme_id": "geo_oil",
            "event_type": "geopolitics",
            "direction": 1,
            "headline": "中东冲突油价跳涨",
            "matched_keywords": ["冲突"],
            "intensity": 1.0,
            "confidence": 0.8,
            "price_confirmed": True,
            "confirmed_by": "SC原油",
            "published_at": "2026-09-09T07:00:00+00:00",
        },
    ]
    ingest = ledger.record_run(
        records=records,
        now=now,
        proxy_price_by_theme={"geo_oil": 520.0},
    )
    assert ingest.inserted == 1
    assert ingest.deduplicated == 1

    # T+1：代理价格上涨 3%（方向 +1 → effective）
    later = now + timedelta(hours=25)
    ledger.record_run(records=[], now=later, proxy_price_by_theme={"geo_oil": 535.6})
    effectiveness = ledger.effectiveness_summary()
    assert effectiveness.matured_1d == 1
    assert effectiveness.hit_rate_1d == 1.0
    assert effectiveness.confirmed_hit_rate_3d is None  # 3d 未成熟
    by_theme = {item["theme_id"]: item for item in effectiveness.by_theme}
    assert by_theme["geo_oil"]["confirmed"] == 1


def test_theme_ledger_confirmed_only_hit_rate_excludes_unconfirmed(
    tmp_path: Path,
) -> None:
    """升级门槛的 hit_rate 只统计价格确认激活事件（未确认不稀释分母）。"""
    ledger = ThemeEventLedger(
        db_path=tmp_path / "ledger.duckdb",
        archive_dir=tmp_path / "archive",
        ttl_days=14,
    )
    now = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    records = [
        {
            "theme_id": "geo_oil",
            "headline": f"确认事件{i}",
            "direction": 1,
            "intensity": 1.0,
            "confidence": 0.8,
            "price_confirmed": True,
        }
        for i in range(2)
    ] + [
        {
            "theme_id": "heat_power",
            "headline": f"未确认事件{i}",
            "direction": 1,
            "intensity": 1.0,
            "confidence": 0.8,
            "price_confirmed": False,
        }
        for i in range(2)
    ]
    ledger.record_run(
        records=records,
        now=now,
        # 代理价在首次入账时锚定（与 m7 语义一致：reference_price 锚定于 first_seen）
        proxy_price_by_theme={"geo_oil": 100.0, "heat_power": 100.0},
    )
    # T+3：geo_oil 代理价上涨（确认事件命中），heat_power 代理价下跌（未确认事件失败）
    later = now + timedelta(hours=73)
    ledger.record_run(
        records=[],
        now=later,
        proxy_price_by_theme={"geo_oil": 110.0, "heat_power": 90.0},
    )
    effectiveness = ledger.effectiveness_summary()
    # 全体 hit_rate_3d = 2/4（含未确认的失败事件拉低）
    assert effectiveness.hit_rate_3d == 0.5
    # 门槛口径 confirmed-only = 2/2
    assert effectiveness.confirmed_matured_3d == 2
    assert effectiveness.confirmed_hit_rate_3d == 1.0


def test_theme_ledger_ttl_archive(tmp_path: Path) -> None:
    ledger = ThemeEventLedger(
        db_path=tmp_path / "ledger.duckdb",
        archive_dir=tmp_path / "archive",
        ttl_days=1,
    )
    now = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    ledger.record_run(
        records=[
            {
                "theme_id": "heat_power",
                "headline": "高温预警",
                "direction": 1,
                "intensity": 1.0,
                "confidence": 0.7,
            }
        ],
        now=now,
        proxy_price_by_theme={},
    )
    # 3 天后过期归档
    late = now + timedelta(days=3)
    ingest = ledger.record_run(records=[], now=late, proxy_price_by_theme={})
    assert ingest.archived == 1
    assert (tmp_path / "archive").exists()


# ---------------------------------------------------------------------------
# scorer
# ---------------------------------------------------------------------------


def _write_state(
    path: Path,
    *,
    mode: str,
    boost: dict[str, float],
    generated_at: str,
    dry_run: bool,
) -> None:
    payload = {
        "generated_at": generated_at,
        "mode": mode,
        "dry_run": dry_run,
        "active_themes": ["geo_oil"],
        "boost_by_symbol": boost,
        "pinned_pool": list(boost.keys())[:10],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _theme_config(state_path: Path) -> MacroThemeConfig:
    return MacroThemeConfig(mode="boost", state_path=str(state_path))


def test_theme_boost_provider_shadow_returns_zero(tmp_path: Path) -> None:
    state_path = tmp_path / "theme_state.json"
    _write_state(
        state_path,
        mode="shadow",
        boost={"600011": 0.8},
        generated_at=datetime.now(tz=UTC).isoformat(),
        dry_run=True,
    )
    provider = ThemeBoostProvider(config=_theme_config(state_path))
    assert _score(provider, "600011") == 0.0
    assert provider.available("600011") is False


def test_theme_boost_provider_boost_mode(tmp_path: Path) -> None:
    state_path = tmp_path / "theme_state.json"
    _write_state(
        state_path,
        mode="boost",
        boost={"600011": 0.8, "600027": 0.4},
        generated_at=datetime.now(tz=UTC).isoformat(),
        dry_run=False,
    )
    provider = ThemeBoostProvider(config=_theme_config(state_path))
    assert provider.available("600011") is True
    assert _score(provider, "600011") == 0.8
    assert provider.available("000001") is False


def test_theme_boost_provider_stale_state_returns_zero(tmp_path: Path) -> None:
    state_path = tmp_path / "theme_state.json"
    _write_state(
        state_path,
        mode="boost",
        boost={"600011": 0.9},
        generated_at=(datetime.now(tz=UTC) - timedelta(days=10)).isoformat(),
        dry_run=False,
    )
    provider = ThemeBoostProvider(config=_theme_config(state_path))
    assert provider.available("600011") is False
    assert _score(provider, "600011") == 0.0


def test_theme_boost_provider_missing_state(tmp_path: Path) -> None:
    provider = ThemeBoostProvider(
        config=_theme_config(tmp_path / "nonexistent.json")
    )
    assert provider.available("600011") is False
    assert _score(provider, "600011") == 0.0


def test_neutral_theme_boost_provider_constant_zero() -> None:
    provider = NeutralThemeBoostProvider()
    assert _score(provider, "600011") == 0.0
    assert provider.available("600011") is False


def test_build_theme_pool_dedup_and_cap() -> None:
    pool = build_theme_pool(
        active_theme_symbols={
            "geo_oil": ["600011", "600027", "600011"],
            "heat_power": ["600027", "600886"],
        },
        pinned_max=2,
    )
    assert pool == ["600011", "600027"]


# ---------------------------------------------------------------------------
# config 集成
# ---------------------------------------------------------------------------


def test_default_config_theme_block_and_weights() -> None:
    config: StockAnalyzerConfig = load_config()
    assert config.theme.mode == "shadow"
    assert config.theme.enabled is False
    assert config.theme.pinned_max_per_day == 10
    assert config.theme.confirmation.price_move_1d_min == 0.015
    assert config.score.weights["theme_boost"] == 0.0
    assert config.strategy_scores["trend"].weights["theme_boost"] == 0.0
    assert config.strategy_scores["monster"].weights["theme_boost"] == 0.0
    assert config.scheduler.theme_daily_sync_time == "16:45"


def test_theme_boost_weight_added_when_missing_from_weights() -> None:
    # 旧 YAML 无 theme_boost 键时 field_validator 兜底补 0.0
    config: StockAnalyzerConfig = load_config()
    config_dict = config.model_dump()
    assert config_dict["score"]["weights"]["theme_boost"] == 0.0
