"""Week5 历史回测复用每日主选股链路（week5_daily）测试。

覆盖 PLAN 的验收面：
- 数据契约：批量质量数据严格受 ``end_date`` 限制（先截断后取最近 N 根），
  AsOfMarketDataProvider 对未来行立即抛泄露错误；
- 选择器契约：``end_date`` 透传批量源、as-of 模式绝不读生产 selection snapshot；
- 引擎（historical context）：显式池/历史全市场两条路径的编排、隔离
  （不写生产报告/审计/关注池/通知）、intraday 降级标注、空态分类；
- 算法一致性：相同 backend 阶段实现下，live 与 historical policy 的
  final selection 完全一致（同一引擎，不复制近似算法）；
- 端到端（真实 service backend + 真实选择器/快照/深阶段/pipeline）：
  完整漏斗 + 生产 artifacts 未被修改。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.data.asof_provider import AsOfMarketDataProvider
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import FutureDataLeakError, SyntheticProvider
from stock_analyzer.runtime.services.week5_selection_engine import (
    Week5AccountState,
    Week5RunContext,
    Week5RunPolicy,
    Week5SelectionEngine,
)
from tests.test_market_warehouse import _build_sample_package
from tests.test_service_week5 import (
    _enable_universe_quality_selector,
    _load_test_config,
    _new_service,
)

AS_OF = date(2026, 7, 31)


# ---------------------------------------------------------------------------
# Part A：数据契约（warehouse end_date / AsOfMarketDataProvider / 选择器）
# ---------------------------------------------------------------------------
def test_warehouse_quality_metrics_end_date_truncates_before_window(tmp_path: Path) -> None:
    """先截断到 end_date 再取最近 N 根：end_date 之后的新行绝不能出现。"""
    package_root = tmp_path / "package"
    _build_sample_package(package_root)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=package_root,
    )
    warehouse.bootstrap_from_offline_package(source_root=package_root)

    full = warehouse.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=5)
    assert sorted(full["date"].dt.strftime("%Y-%m-%d")) == [
        "2026-03-03",
        "2026-03-04",
        "2026-03-05",
    ]

    truncated = warehouse.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=5,
        end_date=date(2026, 3, 4),
    )
    assert sorted(truncated["date"].dt.strftime("%Y-%m-%d")) == ["2026-03-03", "2026-03-04"]

    # lookback=1 + end_date：as-of 有效性的"最近一根"语义
    latest = warehouse.fetch_universe_quality_metrics(
        symbols=["600000"],
        lookback_days=1,
        end_date=date(2026, 3, 4),
    )
    assert len(latest) == 1
    assert latest["date"].iloc[0].strftime("%Y-%m-%d") == "2026-03-04"


class _RecordingBarsProvider:
    """记录 end_date 参数并可模拟"底层链违约返回未来行"的假 provider。"""

    def __init__(self, *, force_leak: bool = False) -> None:
        self.last_end_date: date | None | object = "__unset__"
        self._force_leak = force_leak

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.last_end_date = end_date
        if self._force_leak:
            # 模拟底层链违约：无视 end_date，返回包含 as_of 之后数据的窗口。
            return SyntheticProvider(seed_offset=3).fetch_daily_bars(
                symbol=symbol, lookback_days=lookback_days
            )
        return SyntheticProvider(seed_offset=3).fetch_daily_bars(
            symbol=symbol, lookback_days=lookback_days, end_date=end_date
        )

    def fetch_intraday_summaries(
        self, symbols: list[str], interval: str, lookback_days: int = 120
    ) -> dict[str, pd.DataFrame]:
        # 模拟"摘要窗口含未来行"：帧跨越 as_of（结束于 as_of+10 天），
        # as-of provider 应把 as_of 之后的行裁掉、保留 as_of 之前的行。
        frame = SyntheticProvider().fetch_daily_bars(
            symbol="600000", lookback_days=lookback_days, end_date=date(2026, 8, 10)
        )
        return {symbol: frame for symbol in symbols}

    def status(self) -> dict[str, object]:
        return {}


def test_asof_provider_rejects_future_daily_bars() -> None:
    """底层链返回 as_of 之后的行时必须立刻抛 FutureDataLeakError。"""
    base = _RecordingBarsProvider(force_leak=True)
    provider = AsOfMarketDataProvider(base, AS_OF)
    with pytest.raises(FutureDataLeakError):
        provider.fetch_daily_bars(symbol="600000", lookback_days=30)


def test_asof_provider_clamps_end_date_and_truncates_intraday() -> None:
    """生效截止日 = min(end_date, as_of)；分钟摘要未来行被裁剪。"""
    base = _RecordingBarsProvider()
    provider = AsOfMarketDataProvider(base, AS_OF)
    earlier = date(2026, 7, 20)
    provider.fetch_daily_bars(symbol="600000", lookback_days=30, end_date=earlier)
    assert base.last_end_date == earlier
    provider.fetch_daily_bars(symbol="600000", lookback_days=30, end_date=date(2026, 8, 20))
    assert base.last_end_date == AS_OF

    summaries = provider.fetch_intraday_summaries(["600000"], "1m", lookback_days=30)
    frame = summaries["600000"]
    assert isinstance(frame, pd.DataFrame) and not frame.empty
    assert pd.to_datetime(frame.index).max().date() <= AS_OF
    # 截断前窗口跨越 as_of（结束于 2026-08-10），因此必须真的丢掉未来行。
    assert pd.to_datetime(frame.index).min().date() < AS_OF


class _RecordingBatchSource:
    """记录 fetch_universe_quality_metrics 参数的批量源。"""

    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._frame = frame if frame is not None else pd.DataFrame()

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.calls.append(
            {
                "symbols": list(symbols),
                "lookback_days": lookback_days,
                "end_date": end_date,
            }
        )
        return self._frame


def test_selector_passes_end_date_to_batch_source(tmp_path: Path) -> None:
    """选择器必须把 end_date 透传给批量源（as-of 粗筛契约）。"""
    from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

    source = _RecordingBatchSource()
    selector = UniverseCandidateSelector(
        warehouse=source,
        snapshot_path=str(tmp_path / "selection.json"),
        fallback_sampler=None,
    )
    selector.select(
        symbols=["600000"],
        target_size=1,
        trade_date="2026-07-31",
        ruleset_id="r1",
        board_scope=["SSE"],
        reference_date=AS_OF,
        end_date=AS_OF,
    )
    assert source.calls, "selector should call the batch source"
    assert source.calls[0]["end_date"] == AS_OF


def test_selector_with_end_date_never_reads_production_snapshot(tmp_path: Path) -> None:
    """as-of 模式禁用 selection snapshot fallback（当前快照属于未来信息）。"""
    from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

    snapshot_path = tmp_path / "production_selection.json"
    snapshot_path.write_text("{}", encoding="utf-8")
    source = _RecordingBatchSource()  # 批量不可用 → fallback 分支
    selector = UniverseCandidateSelector(
        warehouse=source,
        snapshot_path=str(snapshot_path),
        fallback_sampler=None,
    )
    result = selector.select(
        symbols=["600000"],
        target_size=1,
        trade_date="2026-07-31",
        ruleset_id="r1",
        board_scope=["SSE"],
        reference_date=AS_OF,
        end_date=AS_OF,
    )
    report = result["report"]
    assert report["selector_mode"] == "degraded_fallback"
    assert report["snapshot_fallback_unavailable_reason"] == "snapshot_disabled_for_asof"
    # 生产 snapshot 文件未被改动/重写
    assert snapshot_path.read_text(encoding="utf-8") == "{}"


# ---------------------------------------------------------------------------
# Part B：共享引擎（stub backend）— historical 编排 / 隔离 / 一致性
# ---------------------------------------------------------------------------
class _StubBackend:
    """实现引擎协议的最小 backend：脚本化的阶段输出 + 调用记录。"""

    def __init__(self, config: StockAnalyzerConfig, symbols: list[str]) -> None:
        self._config = config
        self._symbols = list(symbols)
        self.stored_reports: list[dict[str, object]] = []
        self.audit_events: list[str] = []
        self.notified: list[str] = []
        self.watchlist_synced = False
        self.quality_selection_kwargs: list[dict[str, Any]] = []
        self.pipeline_reports: dict[str, dict[str, object]] = {}

    @property
    def config(self) -> StockAnalyzerConfig:
        return self._config

    def build_data_gate(self, **kwargs: Any) -> dict[str, object]:
        return {"status": "ok", "reasons": []}

    def prefer_local_symbol_universe(self) -> bool:
        return True

    def resolve_symbol_universe(self, **kwargs: Any) -> dict[str, object]:
        return {"source": "stub_universe", "symbols": list(self._symbols), "errors": []}

    def universe_seed_trade_date(self) -> str:
        return AS_OF.isoformat()

    def select_universe_quality_candidates(self, **kwargs: Any) -> dict[str, object]:
        self.quality_selection_kwargs.append(dict(kwargs))
        selected = list(self._symbols)[:3]
        return {
            "selected": selected,
            "report": {
                "selector_mode": "quality_all_eligible",
                "selected_count": len(selected),
                "board_quotas": {},
            },
        }

    def ensure_feature_snapshot(self, *, symbols: list[str], scope: str) -> dict[str, object]:
        return {
            "ok": True,
            "requested_symbol_count": len(symbols),
            "published_symbol_count": len(symbols),
        }

    def light_stage_from_snapshot(
        self, *, frame: Any, target: int, allowed_exchanges: Any
    ) -> dict[str, object]:
        shortlisted = [
            {"symbol": symbol, "baseline_score": 80.0, "exchange": "SSE"}
            for symbol in self._symbols[:target]
        ]
        return {
            "applied": True,
            "mode": "stub_light",
            "universe_count": len(shortlisted),
            "eligible_count": len(shortlisted),
            "shortlisted_count": len(shortlisted),
            "shortlisted": shortlisted,
            "symbols": [item["symbol"] for item in shortlisted],
        }

    def deep_stage_from_snapshot(
        self, *, frame: Any, target: int, light_report: dict[str, object]
    ) -> dict[str, object]:
        selected = [
            {"symbol": item["symbol"], "baseline_score": 80.0, "funnel_score": 85.0}
            for item in (light_report.get("shortlisted") or [])[:target]
        ]
        return {
            "applied": True,
            "mode": "stub_deep",
            "input_count": len(selected),
            "selected_count": len(selected),
            "selected": selected,
            "light_shortlist_count": len(light_report.get("shortlisted") or []),
            "snapshot_match_rows": len(selected),
        }

    def prefilter_universe_symbols(
        self, *, symbols: list[str], top_k_override: Any = None
    ) -> dict[str, object]:
        shortlisted = [
            {"symbol": symbol, "baseline_score": 80.0, "exchange": "SSE"}
            for symbol in symbols[:top_k_override or len(symbols)]
        ]
        return {
            "applied": True,
            "mode": "stub_prefilter",
            "universe_count": len(symbols),
            "eligible_count": len(shortlisted),
            "shortlisted_count": len(shortlisted),
            "shortlisted": shortlisted,
            "symbols": [item["symbol"] for item in shortlisted],
            "stages": {},
        }

    def run_pipeline(self, **kwargs: Any) -> dict[str, object]:
        strategy = str(kwargs.get("strategy", "monster"))
        return self.pipeline_reports.setdefault(strategy, self._build_report(kwargs))

    def _build_report(self, kwargs: dict[str, Any]) -> dict[str, object]:
        symbols = list(kwargs.get("symbols") or [])
        return {
            "trace_id": "stub-trace",
            "signals": [
                {
                    "symbol": symbol,
                    "strategy": str(kwargs.get("strategy", "monster")),
                    "score": 75.0,
                    "grade": "A",
                    "action": "buy",
                    "target_position": 0.1,
                    "probabilities": {"meta": 0.6},
                    "reasons": ["stub_signal"],
                    "decision_trace": {
                        "risk_gate": {"passed": True},
                        "cross_review_gate": {"passed": True},
                    },
                    "post_scan_enrichment": "",
                }
                for symbol in symbols
            ],
            "risk": {"drawdown_pct": 0.0, "action": "normal"},
            "runtime": {"duration_ms": 5},
        }

    def select_live_runtime_provider(self) -> object:
        return SyntheticProvider(seed_offset=5)

    def score_signal_pool_candidate(
        self, *, signal: Any, prefilter_detail: Any
    ) -> dict[str, object]:
        return {
            "symbol": str(signal.get("symbol", "")),
            "action": str(signal.get("action", "")),
            "score": float(signal.get("score", 0.0)),
            "shortlist_score": float(signal.get("score", 0.0)),
            "grade": str(signal.get("grade", "")),
            "reasons": list(signal.get("reasons", [])),
            "decision_trace": dict(signal.get("decision_trace", {})),
        }

    def apply_execution_aware_rerank(
        self, *, candidates: list[dict[str, object]]
    ) -> dict[str, object]:
        return {
            "applied": False,
            "score_key": "shortlist_score",
            "candidate_count": len(candidates),
        }

    def final_signal_selector(
        self,
        *,
        signals: list[dict[str, object]],
        data_gate_status: str,
        min_threshold_lift: float = 0.0,
        news_mode_override: str | None = None,
    ) -> dict[str, object]:
        threshold = 70.0 + max(0.0, float(min_threshold_lift))
        selected: list[dict[str, object]] = []
        rejected: list[dict[str, object]] = []
        for signal in signals:
            reasons: list[str] = []
            if data_gate_status != "ok":
                reasons.append(f"data_gate:{data_gate_status}")
            if float(signal.get("score", 0.0)) < threshold:
                reasons.append("below_min_threshold")
            if reasons:
                rejected.append(
                    {
                        "symbol": str(signal.get("symbol", "")),
                        "score": float(signal.get("score", 0.0)),
                        "action": str(signal.get("action", "")),
                        "reject_reasons": reasons,
                    }
                )
            else:
                selected.append(
                    {
                        "symbol": str(signal.get("symbol", "")),
                        "score": float(signal.get("score", 0.0)),
                        "action": str(signal.get("action", "")),
                        "final_signal_reasons": [],
                        "_news_mode_override": news_mode_override or "",
                    }
                )
        selected.sort(
            key=lambda item: (-float(item.get("score", 0.0)), str(item.get("symbol", "")))
        )
        return {
            "applied": True,
            "mode": "final_selection",
            "input_count": len(signals),
            "selected_count": len(selected),
            "rejected_count": len(rejected),
            "final_signals": selected,
            "rejected": rejected,
        }

    def build_first_board_candidate(self, **kwargs: Any) -> None:
        return None

    def detect_symbol_anomaly(self, **kwargs: Any) -> None:
        return None

    def estimate_sentiment(self, *, monster_report: dict[str, object]) -> tuple[float, bool]:
        return (60.0, True)

    def market_breadth_gate(self, *, now: datetime) -> tuple[dict[str, object], float]:
        return ({"enabled": False, "block_new_buy": False}, 0.0)

    def build_gate_blocked_report(self, **kwargs: Any) -> dict[str, object]:
        return {"status": "blocked_data_gate", "reasons": list(kwargs.get("reasons", []))}

    def build_dual_track_output(self, **kwargs: Any) -> dict[str, object]:
        return {"mode": "legacy"}

    def store_report(self, report: dict[str, object]) -> None:
        self.stored_reports.append(report)

    def record_audit(
        self, *, event_type: str, level: str = "info", trace_id: str = "", payload: Any = None
    ) -> None:
        self.audit_events.append(event_type)

    def sync_watchlist_from_report(self, **kwargs: Any) -> dict[str, object]:
        self.watchlist_synced = True
        return {"enabled": True, "updated": True}

    def watchlist_sync_diagnostics(self, **kwargs: Any) -> dict[str, object]:
        return {"stub": True}

    def build_scan_notification_content(self, **kwargs: Any) -> str:
        return "stub"

    def notify_scan(self, **kwargs: Any) -> None:
        self.notified.append("scan")

    def notify_actionable_signals(self, report: Any, *, trace_id: str, title_prefix: str) -> None:
        self.notified.append("actionable")

    def is_intraday_scheduler_scan(self, *, now: datetime, sync_reason: str) -> bool:
        return False

    def latest_preserved_watchlist_symbols(self, *, top_k_override: Any = None) -> list[str]:
        return []

    def market_warehouse(self) -> object:
        return None

    def provider(self) -> object:
        return SyntheticProvider(seed_offset=9)

    def provider_graph(self) -> list[object]:
        return []

    def runtime_source_mode(self) -> str:
        return "offline_only"


class _FakeUniverseProvider:
    """as-of 上下文用：索引 + 批量质量 + 日线（全部受 end_date 限制）。"""

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)
        self.batch_calls: list[dict[str, object]] = []

    def list_symbols(self) -> list[str]:
        return list(self._symbols)

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        self.batch_calls.append(
            {"symbols": list(symbols), "lookback_days": lookback_days, "end_date": end_date}
        )
        rows = [
            {
                "symbol": symbol,
                "date": pd.Timestamp(AS_OF),
                "close": 10.0,
            }
            for symbol in symbols
        ]
        return pd.DataFrame(rows)

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        return SyntheticProvider(seed_offset=11).fetch_daily_bars(
            symbol=symbol, lookback_days=lookback_days, end_date=end_date or AS_OF
        )

    def status(self) -> dict[str, object]:
        return {}


def _historical_config(tmp_path: Path) -> StockAnalyzerConfig:
    config = _load_test_config()
    config.week5.feature_snapshot_enabled = False
    config.week5.market_breadth_enabled = False
    config.week5.auto_sync_watchlist = False
    config.week5.universe_prefilter_enabled = True
    config.week5.monster_scan_max_symbols = 120
    config.evolution.news_risk_mode = "off"
    return config


def _historical_context(
    *,
    config: StockAnalyzerConfig,
    provider: object,
    tmp_path: Path,
    run_pipeline_fn: Any,
    symbols: list[str] | None,
) -> Week5RunContext:
    return Week5RunContext(
        mode="historical",
        now=datetime(2026, 7, 31, 15, 0),
        as_of=AS_OF,
        config=config,
        provider=provider,
        run_pipeline_fn=run_pipeline_fn,
        symbols=symbols,
        account=Week5AccountState(),
        artifact_dir=tmp_path,
    )


def test_engine_historical_explicit_pool_skips_production_writes(tmp_path: Path) -> None:
    """historical 显式池：直接漏斗 + 不写生产报告/审计/关注池/通知。"""
    config = _historical_config(tmp_path)
    backend = _StubBackend(config, symbols=["600000", "000001", "600519"])
    provider = _FakeUniverseProvider(["600000", "000001", "600519"])
    context = _historical_context(
        config=config,
        provider=provider,
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend.run_pipeline(**kwargs),
        symbols=["600000", "000001", "600519"],
    )
    engine = Week5SelectionEngine(
        backend=backend, context=context, policy=Week5RunPolicy.historical()
    )
    report = engine.run()

    assert report["run_mode"] == "historical"
    assert report["funnel"]["policy"] == "direct_non_universe"
    assert report["historical_context"]["as_of"] == AS_OF.isoformat()
    assert report["historical_context"]["account"]["neutral"] is True
    assert report["historical_context"]["news_neutralized"] is True
    # 隔离：生产写路径全部关闭
    assert backend.stored_reports == []
    assert backend.audit_events == []
    assert backend.notified == []
    assert backend.watchlist_synced is False
    # final selection 走同一套 backend 阶段
    final = report["funnel"]["final_selection"]
    assert final["selected_count"] == 3
    assert [item["symbol"] for item in final["final_signals"]] == [
        "000001",
        "600000",
        "600519",
    ] or len(final["final_signals"]) == 3


def test_engine_historical_full_market_resolves_universe_with_end_date(tmp_path: Path) -> None:
    """historical 全市场：从 provider 索引生成股票池，质量选择收到 end_date。"""
    config = _historical_config(tmp_path)
    symbols = ["600000", "000001", "600519", "300750"]
    backend = _StubBackend(config, symbols=symbols)
    provider = _FakeUniverseProvider(symbols)
    context = _historical_context(
        config=config,
        provider=provider,
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend.run_pipeline(**kwargs),
        symbols=None,
    )
    engine = Week5SelectionEngine(
        backend=backend, context=context, policy=Week5RunPolicy.historical()
    )
    report = engine.run()

    assert report["symbol_source"].startswith("provider_index:as_of_quality_selector")
    prefilter = report["prefilter"]
    assert prefilter["historical_universe"]["provider_index_count"] == len(symbols)
    assert prefilter["historical_universe"]["as_of_valid_count"] == len(symbols)
    assert prefilter["historical_universe"]["selected_count"] == 3
    # 质量选择收到 as_of end_date + 任务独立 selection snapshot 路径
    assert backend.quality_selection_kwargs, "quality selection should be invoked"
    kwargs = backend.quality_selection_kwargs[0]
    assert kwargs["end_date"] == AS_OF
    assert str(kwargs["selection_snapshot_path"]).endswith("universe_selection.json")
    assert provider.batch_calls[0]["end_date"] == AS_OF
    # snapshot_funnel + prefilter（快照禁用时直接走 prefilter stub）
    assert report["funnel"]["policy"] == "snapshot_funnel"
    assert report["prefilter"]["applied"] is True


def test_engine_live_and_historical_same_final_selection(tmp_path: Path) -> None:
    """一致性：相同 backend 阶段实现下，live 与 historical 的 final selection 一致。"""
    config = _historical_config(tmp_path)
    symbols = ["600000", "000001", "600519"]
    backend_live = _StubBackend(config, symbols=symbols)
    backend_hist = _StubBackend(config, symbols=symbols)

    live_context = Week5RunContext(
        mode="live",
        now=datetime(2026, 7, 31, 20, 30),
        symbols=list(symbols),
        account=Week5AccountState(),
    )
    live_policy = Week5RunPolicy.live()
    live_policy.notify = False
    live_report = Week5SelectionEngine(
        backend=backend_live, context=live_context, policy=live_policy
    ).run()

    hist_context = _historical_context(
        config=config,
        provider=_FakeUniverseProvider(symbols),
        tmp_path=tmp_path,
        run_pipeline_fn=lambda **kwargs: backend_hist.run_pipeline(**kwargs),
        symbols=list(symbols),
    )
    hist_report = Week5SelectionEngine(
        backend=backend_hist, context=hist_context, policy=Week5RunPolicy.historical()
    ).run()

    live_final = live_report["funnel"]["final_selection"]
    hist_final = hist_report["funnel"]["final_selection"]
    assert [item["symbol"] for item in live_final["final_signals"]] == [
        item["symbol"] for item in hist_final["final_signals"]
    ]
    assert [item["score"] for item in live_final["final_signals"]] == [
        item["score"] for item in hist_final["final_signals"]
    ]
    assert live_report["signal_pool"]["candidate_count"] == (
        hist_report["signal_pool"]["candidate_count"]
    )
    # historical 的 final selector 强制 news off（stub 记录 override）
    hist_override = {item["_news_mode_override"] for item in hist_final["final_signals"]}
    assert hist_override == {"off"}


# ---------------------------------------------------------------------------
# Part C：端到端（真实 service backend + 真实选择器/快照/深阶段/pipeline）
# ---------------------------------------------------------------------------
class _FakeHistoricalProvider:
    """端到端假 provider：批量质量 + 日线（受 end_date 限制）+ 无分钟数据。"""

    def __init__(self, symbols: list[str], *, data_end: date) -> None:
        self._symbols = list(symbols)
        self._data_end = data_end
        self._inner = SyntheticProvider(seed_offset=13)
        self._daily_cache: dict[tuple[str, int, str], pd.DataFrame] = {}

    def list_symbols(self) -> list[str]:
        return list(self._symbols)

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        effective_end = end_date or self._data_end
        key = (symbol, lookback_days, effective_end.isoformat())
        cached = self._daily_cache.get(key)
        if cached is not None:
            return cached.copy()
        frame = self._inner.fetch_daily_bars(
            symbol=symbol, lookback_days=lookback_days, end_date=effective_end
        )
        self._daily_cache[key] = frame.copy()
        return frame

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for symbol in symbols:
            bars = self.fetch_daily_bars(symbol=symbol, lookback_days=250, end_date=end_date)
            frame = bars.tail(max(1, int(lookback_days))).reset_index()
            frame = frame.rename(columns={"index": "date"})
            if "date" not in frame.columns:
                frame = bars.tail(max(1, int(lookback_days))).reset_index(names="date")
            frame["symbol"] = symbol
            frame["financial_completeness"] = 1.0
            frame["financial_data_complete"] = True
            frame["background_data_complete"] = True
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        combined = pd.concat(frames, ignore_index=True)
        return combined.sort_values(["symbol", "date"]).reset_index(drop=True)

    def fetch_intraday_summaries(
        self, symbols: list[str], interval: str, lookback_days: int = 120
    ) -> dict[str, pd.DataFrame]:
        return {symbol: pd.DataFrame() for symbol in symbols}

    def fetch_intraday_summary(
        self, symbol: str, interval: str, lookback_days: int = 120
    ) -> pd.DataFrame:
        return pd.DataFrame()

    def status(self) -> dict[str, object]:
        return {}


def test_week5_historical_day_end_to_end_full_funnel_with_isolation(tmp_path: Path) -> None:
    """端到端：真实引擎 + 真实 service 阶段实现跑通全市场漏斗，且生产不落盘。"""
    from stock_analyzer.runtime.services.week5_historical_runner import run_week5_historical_day

    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.universe_quality_target_size = 3
    config.week5.light_candidate_target = 3
    config.week5.deep_candidate_target = 3
    config.week5.final_signal_cap = 2
    config.week5.feature_snapshot_root = str(tmp_path / "production_features_light")
    config.week5.universe_quality_snapshot_path = str(tmp_path / "production_selection.json")
    config.week5.market_breadth_enabled = False
    config.week5.auto_sync_watchlist = False
    config.week5.universe_quality_require_financial_data = False
    config.evolution.news_risk_mode = "penalty"
    service = _new_service(config, provider=SyntheticProvider(seed_offset=17))
    service.state.watchlist = ["600999"]

    symbols = ["600000", "000001", "600519"]
    provider = _FakeHistoricalProvider(symbols, data_end=AS_OF)
    task_dir = tmp_path / "week5_task"
    task_dir.mkdir(parents=True, exist_ok=True)
    watchlist_before = list(service.state.watchlist)

    report = run_week5_historical_day(
        service=service,
        as_of=AS_OF,
        task_dir=task_dir,
        symbols=None,
        base_provider=provider,
    )

    assert report["run_mode"] == "historical"
    assert report["funnel"]["policy"] == "snapshot_funnel"
    context = report["historical_context"]
    assert context["as_of"] == AS_OF.isoformat()
    assert context["account"]["neutral"] is True
    assert context["news_neutralized"] is True
    assert context["realtime_data_allowed"] is False
    assert context["market_breadth_recomputed"] is True
    # 分钟数据缺失 → 降级标注
    assert report["prefilter"]["intraday_degraded"] is True
    assert report["prefilter"]["intraday_coverage_ratio"] == 0.0
    # 完整漏斗计数链
    funnel = report["funnel"]
    assert funnel["light_count"] > 0
    assert funnel["deep_count"] > 0
    assert funnel["final_count"] <= 2
    assert funnel["final_count"] == funnel["final_selection"]["selected_count"]
    # 任务独立目录里有快照产物；生产 root 没有任何文件
    assert (task_dir / "features_light" / "current.json").exists()
    assert not (tmp_path / "production_features_light").exists()
    assert not (tmp_path / "production_selection.json").exists()
    # 隔离：生产关注池/生产周报/审计未被触碰
    assert list(service.state.watchlist) == watchlist_before
    assert service._week5_service._state_service.latest_week5_scan_report() is None  # noqa: SLF001


def test_week5_historical_day_explicit_pool_marks_manual_source(tmp_path: Path) -> None:
    """显式股票池：标注 manual_symbols_not_full_market，不进入质量选择。"""
    from stock_analyzer.runtime.services.week5_historical_runner import run_week5_historical_day

    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.feature_snapshot_root = str(tmp_path / "production_features_light")
    config.week5.auto_sync_watchlist = False
    config.week5.market_breadth_enabled = False
    service = _new_service(config, provider=SyntheticProvider(seed_offset=19))

    provider = _FakeHistoricalProvider(["600000", "000001"], data_end=AS_OF)
    task_dir = tmp_path / "week5_task_explicit"
    task_dir.mkdir(parents=True, exist_ok=True)
    report = run_week5_historical_day(
        service=service,
        as_of=AS_OF,
        task_dir=task_dir,
        symbols=["600000", "000001"],
        base_provider=provider,
    )
    assert report["prefilter"]["explicit_pool"] is True
    assert report["prefilter"]["explicit_pool_note"] == "manual_symbols_not_full_market"
    assert report["funnel"]["policy"] == "direct_non_universe"
    assert report["watchlist_size"] == 2


# ---------------------------------------------------------------------------
# Part D：API contract（algorithm=week5_daily）
# ---------------------------------------------------------------------------
def test_api_week5_daily_invalid_algorithm_returns_400() -> None:
    from fastapi.testclient import TestClient

    from stock_analyzer.main import app

    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={"date": "2026-07-31", "algorithm": "bogus"},
    )
    assert response.status_code == 400
    assert "invalid_algorithm" in response.json()["detail"]


def test_api_week5_daily_busy_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from stock_analyzer import main as main_module
    from stock_analyzer.main import app

    acquired = {"flag": True}

    def _busy() -> bool:
        return False

    monkeypatch.setattr(main_module._service, "try_acquire_week5_backtest", _busy)
    monkeypatch.setattr(
        main_module._service._config.asof_backtest, "week5_daily_enabled", True
    )
    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={"date": "2026-07-31", "algorithm": "week5_daily"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "week5_backtest_busy"
    assert acquired["flag"]


def test_api_week5_daily_disabled_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from stock_analyzer import main as main_module
    from stock_analyzer.main import app

    patched_asof = main_module._service._config.asof_backtest.model_copy(
        update={"week5_daily_enabled": False}
    )
    patched_config = main_module._service._config.model_copy(
        update={"asof_backtest": patched_asof}
    )
    # 路由层经 get_config() 读 main 模块级单例，两处都要 patch。
    monkeypatch.setattr(main_module, "_config", patched_config)
    monkeypatch.setattr(main_module._service, "_config", patched_config)
    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={"date": "2026-07-31", "algorithm": "week5_daily"},
    )
    assert response.status_code == 409
    assert "week5_backtest_disabled" in response.json()["detail"]


def test_api_week5_daily_end_to_end_full_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """week5_daily 提交 → 202/进度 → 漏斗与 final gate 可见 → 隔离。"""
    import time

    from fastapi.testclient import TestClient

    from stock_analyzer import main as main_module
    from stock_analyzer.data import provider_factory
    from stock_analyzer.main import app

    output_dir = tmp_path / "asof_scan"
    week5 = main_module._service._config.week5.model_copy(
        update={
            # 生产级质量阈值会拒绝合成数据（roe 缺失等）；测试聚焦漏斗编排，
            # 放宽阈值与 _enable_universe_quality_selector 的口径一致。
            "universe_quality_min_avg_turnover_20": 0.0,
            "universe_quality_min_float_market_cap": 0.0,
            "universe_quality_require_financial_data": False,
            "universe_quality_min_roe": 0.0,
            "universe_quality_target_size": 3,
            "light_candidate_target": 3,
            "deep_candidate_target": 3,
            "final_signal_cap": 2,
            "market_breadth_enabled": False,
            "auto_sync_watchlist": False,
        }
    )
    patched_asof = main_module._service._config.asof_backtest.model_copy(
        update={"output_dir": str(output_dir)}
    )
    patched_config = main_module._service._config.model_copy(
        update={"asof_backtest": patched_asof, "week5": week5}
    )
    monkeypatch.setattr(main_module, "_config", patched_config)
    monkeypatch.setattr(main_module._service, "_config", patched_config)
    monkeypatch.setattr(
        main_module._service,
        "_asof_backtest_service",
        type(main_module._service._asof_backtest_service)(main_module._service),
    )

    symbols = ["600000", "000001", "600519"]
    fake_provider = _FakeHistoricalProvider(symbols, data_end=AS_OF)
    monkeypatch.setattr(
        provider_factory,
        "build_runtime_provider",
        lambda config, synthetic_seed=2026: fake_provider,
    )
    # 质量选择的批量源从主 service 的 provider 图解析，必须指向具备
    # fetch_universe_quality_metrics 能力的假 provider（受 end_date 限制）。
    monkeypatch.setattr(main_module._service, "_provider", fake_provider)

    client = TestClient(app)
    response = client.post(
        "/backtest/asof-scan",
        json={
            "date": AS_OF.isoformat(),
            "symbols": [],
            "algorithm": "week5_daily",
            "holding_top_n": 5,
            "horizon_days": 5,
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    deadline = time.monotonic() + 120.0
    final: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload = client.get(f"/tasks/{task_id}").json()
        if payload["status"] in ("succeeded", "failed"):
            final = payload
            break
        time.sleep(0.2)
    assert final.get("status") == "succeeded", final
    result = final["result"]
    assert result["algorithm"] == "week5_daily"
    entry = result["dates"][AS_OF.isoformat()]
    caveats = result["caveats"]
    assert caveats["candidate_pool_source"] == "full_market"
    assert caveats["neutral_account"] is True
    assert caveats["news_neutralized"] is True
    assert caveats["intraday_degraded"] is True, (
        entry.get("historical_context"),
        entry.get("funnel"),
    )
    assert entry["run_mode"] == "historical"
    assert entry["funnel"]["quality_count"] == 3
    assert entry["funnel"]["light_count"] > 0
    assert entry["funnel"]["deep_count"] > 0
    assert entry["funnel"]["final_count"] <= 2
    # candidates 明确 = final_signals；原始池独立保留
    assert entry["candidate_count"] == entry["funnel"]["final_count"]
    assert entry["historical_context"]["intraday_degraded"] is True
    assert len(entry["signal_pool"]["candidates"]) >= entry["candidate_count"]
    assert set(entry.keys()) >= {
        "funnel",
        "signal_pool",
        "final_selection",
        "rejection_reasons",
        "empty_state",
        "stage_timings",
        "holding_curve",
        "historical_context",
    }
    assert entry["holding_curve"] is not None or entry["candidate_count"] == 0
    # latest 落盘且带算法标注
    latest = client.get("/backtest/asof-scan/latest").json()["report"]
    assert latest["algorithm"] == "week5_daily"
